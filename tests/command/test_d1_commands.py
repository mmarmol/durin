"""Tests for the new D1 slash commands.

Covers /sessions, /resume, /compact, /copy, /name, /hotkeys —
the read-side and write-side surface added for daily-driver
ergonomics in docs/09_daily_driver_plan.md §D1.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.command.builtin import (
    cmd_compact,
    cmd_copy,
    cmd_hotkeys,
    cmd_name,
    cmd_resume,
    cmd_sessions,
)
from durin.command.router import CommandContext


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=100, temperature=0.1, reasoning_effort=None
    )
    return provider


def _make_loop(tmp_path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=1000,
    )


def _ctx(loop: AgentLoop, raw: str, args: str = "", key: str = "cli:direct") -> CommandContext:
    channel, chat_id = key.split(":", 1) if ":" in key else ("cli", key)
    msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=raw)
    session = loop.sessions.get_or_create(key)
    return CommandContext(msg=msg, session=session, key=key, raw=raw, args=args, loop=loop)


def _make_session(loop: AgentLoop, key: str, messages: list[dict] | None = None) -> None:
    session = loop.sessions.get_or_create(key)
    for msg in messages or []:
        session.messages.append(msg)
    loop.sessions.save(session)


# ---------------------------------------------------------------------------
# /sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sessions_empty_workspace(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    ctx = _ctx(loop, "/sessions")
    out = await cmd_sessions(ctx)
    assert "No sessions" in out.content


@pytest.mark.asyncio
async def test_sessions_lists_existing(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    _make_session(loop, "cli:alpha", [{"role": "user", "content": "hi"}])
    _make_session(loop, "cli:beta", [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ])
    ctx = _ctx(loop, "/sessions", key="cli:alpha")
    out = await cmd_sessions(ctx)
    assert "cli:alpha" in out.content
    assert "cli:beta" in out.content
    assert "← current" in out.content


@pytest.mark.asyncio
async def test_sessions_filter_substring(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    _make_session(loop, "cli:alpha")
    _make_session(loop, "cli:beta")
    ctx = _ctx(loop, "/sessions alpha", args="alpha")
    out = await cmd_sessions(ctx)
    assert "cli:alpha" in out.content
    assert "cli:beta" not in out.content


# ---------------------------------------------------------------------------
# /resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_requires_arg(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    ctx = _ctx(loop, "/resume", args="")
    out = await cmd_resume(ctx)
    assert "Usage" in out.content


@pytest.mark.asyncio
async def test_resume_no_match(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    _make_session(loop, "cli:alpha")
    ctx = _ctx(loop, "/resume nope", args="nope")
    out = await cmd_resume(ctx)
    assert "No session matches" in out.content


@pytest.mark.asyncio
async def test_resume_ambiguous(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    _make_session(loop, "cli:alpha-one")
    _make_session(loop, "cli:alpha-two")
    ctx = _ctx(loop, "/resume alpha", args="alpha")
    out = await cmd_resume(ctx)
    assert "ambiguous" in out.content


@pytest.mark.asyncio
async def test_resume_switches_via_metadata(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    _make_session(loop, "cli:beta")
    ctx = _ctx(loop, "/resume beta", args="beta", key="cli:direct")
    out = await cmd_resume(ctx)
    assert "Switched" in out.content
    assert out.metadata["_switch_chat_id"] == "beta"


@pytest.mark.asyncio
async def test_resume_already_in_session(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    _make_session(loop, "cli:alpha")
    ctx = _ctx(loop, "/resume alpha", args="alpha", key="cli:alpha")
    out = await cmd_resume(ctx)
    assert "Already" in out.content
    assert "_switch_chat_id" not in (out.metadata or {})


# ---------------------------------------------------------------------------
# /compact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_nothing_to_do(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    ctx = _ctx(loop, "/compact")
    out = await cmd_compact(ctx)
    assert "Nothing to compact" in out.content


@pytest.mark.asyncio
async def test_compact_summarises_and_advances_cursor(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:direct")
    session.messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    loop.consolidator.archive = AsyncMock(
        return_value=("a brief summary", {"entities": ["x"], "topics": []})
    )
    ctx = _ctx(loop, "/compact")
    out = await cmd_compact(ctx)
    assert "Compacted 2 messages" in out.content
    assert session.last_consolidated == 2
    # A10: summary lives in `memory/session_summary/<key>.md` now.
    from durin.memory.session_summary_store import get_session_summary
    text, _ = get_session_summary(loop.workspace, session.key)
    assert text == "a brief summary"


@pytest.mark.asyncio
async def test_compact_handles_consolidator_degraded(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:direct")
    session.messages = [{"role": "user", "content": "u1"}]
    loop.consolidator.archive = AsyncMock(
        return_value=(None, {"entities": [], "topics": []})
    )
    ctx = _ctx(loop, "/compact")
    out = await cmd_compact(ctx)
    assert "raw-archived" in out.content
    assert session.last_consolidated == 1  # cursor still advanced


# ---------------------------------------------------------------------------
# /copy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_no_assistant_message(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    ctx = _ctx(loop, "/copy")
    out = await cmd_copy(ctx)
    assert "No assistant message" in out.content


@pytest.mark.asyncio
async def test_copy_falls_back_with_message_when_clipboard_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No clipboard tool → return a helpful error AND the content length."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:direct")
    session.messages = [{"role": "assistant", "content": "the answer is forty-two"}]

    def _no_clipboard(_text: str) -> str:
        raise RuntimeError("no clipboard tool found")

    monkeypatch.setattr("durin.command.builtin._copy_to_clipboard", _no_clipboard)
    ctx = _ctx(loop, "/copy")
    out = await cmd_copy(ctx)
    assert "no clipboard tool found" in out.content
    assert "chars" in out.content


@pytest.mark.asyncio
async def test_copy_invokes_clipboard_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:direct")
    session.messages = [{"role": "assistant", "content": "hello world"}]

    seen: list[str] = []

    def _fake_copy(text: str) -> str:
        seen.append(text)
        return "fake-tool"

    monkeypatch.setattr("durin.command.builtin._copy_to_clipboard", _fake_copy)
    ctx = _ctx(loop, "/copy")
    out = await cmd_copy(ctx)
    assert seen == ["hello world"]
    assert "Copied 11 chars" in out.content
    assert "fake-tool" in out.content


@pytest.mark.asyncio
async def test_copy_handles_list_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some providers return content as a list of text blocks — copy must join them."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:direct")
    session.messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "line one"},
                {"type": "text", "text": "line two"},
            ],
        }
    ]
    captured: list[str] = []
    monkeypatch.setattr(
        "durin.command.builtin._copy_to_clipboard",
        lambda t: (captured.append(t), "tool")[1],
    )
    ctx = _ctx(loop, "/copy")
    await cmd_copy(ctx)
    assert "line one" in captured[0]
    assert "line two" in captured[0]


# ---------------------------------------------------------------------------
# /name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_name_set_then_show(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    ctx = _ctx(loop, "/name my-project", args="my-project")
    out = await cmd_name(ctx)
    assert "set to" in out.content
    session = loop.sessions.get_or_create("cli:direct")
    assert session.metadata["display_name"] == "my-project"

    # Show — empty args
    ctx2 = _ctx(loop, "/name", args="")
    out2 = await cmd_name(ctx2)
    assert "my-project" in out2.content


@pytest.mark.asyncio
async def test_name_rejects_too_long(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    long_name = "x" * 100
    ctx = _ctx(loop, f"/name {long_name}", args=long_name)
    out = await cmd_name(ctx)
    assert "too long" in out.content


@pytest.mark.asyncio
async def test_name_empty_when_unset(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    ctx = _ctx(loop, "/name", args="")
    out = await cmd_name(ctx)
    assert "No display name" in out.content


# ---------------------------------------------------------------------------
# /hotkeys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hotkeys_shows_table(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    ctx = _ctx(loop, "/hotkeys")
    out = await cmd_hotkeys(ctx)
    assert "Keyboard shortcuts" in out.content
    assert "Ctrl+C" in out.content
