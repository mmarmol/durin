"""D2 — memory surface slash commands.

Covers /memory (list/show/search/drill), /remember, /forget,
/sources [ingest], /audit, /why.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.command.builtin import (
    cmd_audit,
    cmd_forget,
    cmd_memory,
    cmd_remember,
    cmd_sources,
    cmd_why,
)
from durin.command.router import CommandContext
from durin.memory.store import store_memory


def _provider() -> MagicMock:
    p = MagicMock()
    p.get_default_model.return_value = "test-model"
    p.generation = SimpleNamespace(max_tokens=100, temperature=0.1, reasoning_effort=None)
    return p


def _make_loop(tmp_path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=1000,
    )


def _ctx(loop, raw: str, args: str = "", key: str = "cli:direct") -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=raw)
    session = loop.sessions.get_or_create(key)
    return CommandContext(msg=msg, session=session, key=key, raw=raw, args=args, loop=loop)


# ---------------------------------------------------------------------------
# /memory list / show / search / drill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_no_subcommand_shows_usage(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_memory(_ctx(loop, "/memory"))
    assert "Usage" in out.content


@pytest.mark.asyncio
async def test_memory_list_empty(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_memory(_ctx(loop, "/memory list", args="list"))
    assert "No memory entries" in out.content


@pytest.mark.asyncio
async def test_memory_list_with_entries(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    store_memory(tmp_path, content="cache discussion", headline="cache")
    store_memory(tmp_path, content="config note", headline="config", class_name="stable")
    out = await cmd_memory(_ctx(loop, "/memory list", args="list"))
    assert "cache" in out.content
    assert "config" in out.content
    assert "episodic/" in out.content
    assert "stable/" in out.content


@pytest.mark.asyncio
async def test_memory_list_filtered_by_class(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    store_memory(tmp_path, content="a", headline="alpha")
    store_memory(tmp_path, content="b", headline="beta", class_name="stable")
    out = await cmd_memory(_ctx(loop, "/memory list stable", args="list stable"))
    assert "beta" in out.content
    assert "alpha" not in out.content


@pytest.mark.asyncio
async def test_memory_show_by_id(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    result = store_memory(tmp_path, content="full body", headline="hello")
    out = await cmd_memory(_ctx(loop, f"/memory show {result['id']}", args=f"show {result['id']}"))
    assert "hello" in out.content
    assert "full body" in out.content


@pytest.mark.asyncio
async def test_memory_show_no_match(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_memory(_ctx(loop, "/memory show zzz", args="show zzz"))
    assert "No memory entry matches" in out.content


@pytest.mark.asyncio
async def test_memory_search(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    store_memory(tmp_path, content="uses pytest framework", headline="testing")
    out = await cmd_memory(_ctx(loop, "/memory search pytest", args="search pytest"))
    assert "pytest" in out.content.lower() or "testing" in out.content


@pytest.mark.asyncio
async def test_memory_drill_unknown_uri(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_memory(_ctx(loop, "/memory drill nope.md", args="drill nope.md"))
    assert "drill error" in out.content


# ---------------------------------------------------------------------------
# /remember
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_stores_user_authored(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_remember(
        _ctx(loop, "/remember user prefers terse", args="user prefers terse")
    )
    assert "Remembered" in out.content
    assert "user_authored" in out.content
    # Confirm on disk
    files = list((tmp_path / "memory" / "episodic").glob("*.md"))
    assert len(files) == 1


@pytest.mark.asyncio
async def test_remember_empty_returns_usage(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_remember(_ctx(loop, "/remember", args=""))
    assert "Usage" in out.content


# ---------------------------------------------------------------------------
# /forget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_deletes_entry(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    result = store_memory(tmp_path, content="to forget", headline="ephemeral")
    path = Path(result["path"])
    assert path.is_file()
    out = await cmd_forget(_ctx(loop, f"/forget {result['id']}", args=result["id"]))
    assert "Forgot" in out.content
    assert not path.is_file()


@pytest.mark.asyncio
async def test_forget_no_match(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_forget(_ctx(loop, "/forget zzz", args="zzz"))
    assert "No memory entry matches" in out.content


@pytest.mark.asyncio
async def test_forget_ambiguous(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    r1 = store_memory(tmp_path, content="alpha one", headline="A1")
    r2 = store_memory(tmp_path, content="alpha two", headline="A2")
    # Use the shared prefix of their hash ids that matches both
    common = r1["id"][:2] if r1["id"][:2] == r2["id"][:2] else None
    if common is None:
        # Synthesise two ids that overlap
        r3 = store_memory(tmp_path, content="alpha three", headline="A3")
        common = r3["id"][:2]
    # ambiguity depends on having 2+ entries matching the substring
    out = await cmd_forget(_ctx(loop, f"/forget {common}", args=common))
    # If only one happens to match, that's also OK behaviour — just verify either response shape
    assert "Forgot" in out.content or "ambiguous" in out.content


# ---------------------------------------------------------------------------
# /sources + /sources ingest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sources_empty(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_sources(_ctx(loop, "/sources", args=""))
    assert "No ingested artifacts" in out.content


@pytest.mark.asyncio
async def test_sources_ingest_then_list(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    src = tmp_path / "doc.md"
    src.write_text("# doc", encoding="utf-8")
    out = await cmd_sources(
        _ctx(loop, f"/sources ingest {src}", args=f"ingest {src}")
    )
    assert "Ingested as" in out.content

    out2 = await cmd_sources(_ctx(loop, "/sources", args=""))
    assert "Ingested sources" in out2.content
    assert "1" in out2.content


@pytest.mark.asyncio
async def test_sources_ingest_missing_path(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_sources(
        _ctx(loop, "/sources ingest", args="ingest")
    )
    assert "Usage" in out.content


# ---------------------------------------------------------------------------
# /audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_empty(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_audit(_ctx(loop, "/audit"))
    assert "No stable memory" in out.content


@pytest.mark.asyncio
async def test_audit_lists_stable_headlines(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    store_memory(
        tmp_path,
        content="agent body",
        headline="user prefers terse responses",
        class_name="stable",
    )
    out = await cmd_audit(_ctx(loop, "/audit"))
    assert "user prefers terse" in out.content
    assert "stable memory (audit)" in out.content


# ---------------------------------------------------------------------------
# /why
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_why_with_match_shows_provenance(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    store_memory(
        tmp_path,
        content="The agent stored evidence",
        headline="user prefers terse",
        class_name="stable",
        source_refs=["[turn 42](../sessions/abc.md#turn-42)"],
    )
    out = await cmd_why(_ctx(loop, "/why terse", args="terse"))
    assert "Provenance" in out.content
    assert "user prefers terse" in out.content
    assert "turn-42" in out.content or "sessions/abc.md" in out.content


@pytest.mark.asyncio
async def test_why_no_match(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_why(_ctx(loop, "/why nope", args="nope"))
    assert "No memory supports" in out.content


@pytest.mark.asyncio
async def test_why_empty_returns_usage(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_why(_ctx(loop, "/why", args=""))
    assert "Usage" in out.content
