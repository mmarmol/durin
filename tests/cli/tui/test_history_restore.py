"""Tests for TUI session-restore behaviour on mount."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from durin.bus.queue import MessageBus
from durin.cli.tui.app import DurinApp
from durin.cli.tui.widgets import ChatView, MessageBubble


def _fake_agent_loop(messages: list[dict]) -> SimpleNamespace:
    async def _idle() -> None:
        await asyncio.Event().wait()

    session = SimpleNamespace(messages=messages, metadata={})
    bus = MessageBus()
    return SimpleNamespace(
        bus=bus,
        workspace="/tmp/durin_test_ws",
        model="glm-5.1",
        model_preset="default",
        context_window_tokens=200_000,
        sessions=SimpleNamespace(get_or_create=lambda k: session),
        tool_names=[],
        run=_idle,
    )


@pytest.mark.asyncio
async def test_restore_renders_last_messages_as_bubbles() -> None:
    """A session with prior messages must replay them on TUI mount."""
    loop = _fake_agent_loop([
        {"role": "user", "content": "first message"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "second message"},
        {"role": "assistant", "content": "second reply"},
    ])
    app = DurinApp(agent_loop=loop, cli_chat_id="restoretest")
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatView)
        bubble_bodies = [b.body for b in chat.query(MessageBubble)]
        # Tail should be present (banner appears too — we check just the
        # restored user/assistant content).
        assert "first message" in bubble_bodies
        assert "second reply" in bubble_bodies


@pytest.mark.asyncio
async def test_restore_shows_hidden_hint_when_history_exceeds_tail() -> None:
    """If history > tail size, an 'N earlier hidden' note must appear."""
    messages = []
    for i in range(20):
        messages.append({"role": "user", "content": f"u{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})
    loop = _fake_agent_loop(messages)
    app = DurinApp(agent_loop=loop, cli_chat_id="bighist")
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatView)
        bodies = [b.body for b in chat.query(MessageBubble)]
        # Default tail is 6 → 34 hidden out of 40.
        hidden_notes = [b for b in bodies if "earlier message" in b and "hidden" in b]
        assert hidden_notes, f"expected a 'hidden' note. bodies: {bodies[:3]}"


@pytest.mark.asyncio
async def test_restore_silent_when_session_empty() -> None:
    """A brand-new session (no messages) shows only the welcome banner."""
    loop = _fake_agent_loop([])
    app = DurinApp(agent_loop=loop, cli_chat_id="fresh")
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatView)
        # Only the banner bubble should exist.
        roles = [b._role for b in chat.query(MessageBubble)]
        # We allow a `banner` bubble + the working indicator is NOT a bubble,
        # so user/assistant should be zero.
        assert "user" not in roles
        assert "assistant" not in roles


@pytest.mark.asyncio
async def test_banner_renders_before_restored_history() -> None:
    """Layout invariant: banner sits at the top, restored history just above
    the input (i.e. AFTER the banner in mount order)."""
    loop = _fake_agent_loop([
        {"role": "user", "content": "first old message"},
        {"role": "assistant", "content": "first old reply"},
    ])
    app = DurinApp(agent_loop=loop, cli_chat_id="orderingtest")
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatView)
        bubbles = list(chat.query(MessageBubble))
        # First bubble is the banner; restored user message comes AFTER.
        roles = [b._role for b in bubbles]
        first_banner = roles.index("banner")
        first_user = roles.index("user")
        assert first_banner < first_user, (
            "banner must mount before the restored history. "
            f"got roles in order: {roles}"
        )


@pytest.mark.asyncio
async def test_restore_handles_block_content_messages() -> None:
    """Multimodal messages (list of blocks) should still surface their text."""
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hello with attachment"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "got it"}]},
    ]
    loop = _fake_agent_loop(messages)
    app = DurinApp(agent_loop=loop, cli_chat_id="multimodal")
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatView)
        bodies = [b.body for b in chat.query(MessageBubble)]
        assert "hello with attachment" in bodies
        assert "got it" in bodies


# ---------------------------------------------------------------------------
# Tool-call replay: tool messages must render as ToolCallBubbles
# ---------------------------------------------------------------------------


import json as _json


@pytest.mark.asyncio
async def test_history_tool_message_becomes_tool_call_bubble() -> None:
    """A `tool` role message with a matching assistant tool_call must
    render as a ToolCallBubble (rich + clickable), not a plain text bubble."""
    from durin.cli.tui.widgets import ToolCallBubble

    messages = [
        {"role": "user", "content": "find mxhero"},
        {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "call_x1", "type": "function",
                "function": {
                    "name": "web_fetch",
                    "arguments": _json.dumps({"url": "https://www.mxhero.com"}),
                },
            }],
        },
        {
            "role": "tool", "tool_call_id": "call_x1", "name": "web_fetch",
            "content": "{\"url\": \"https://www.mxhero.com\", \"status\": 200}",
        },
        {"role": "assistant", "content": "URL: https://www.mxhero.com"},
    ]
    loop = _fake_agent_loop(messages)
    app = DurinApp(agent_loop=loop, cli_chat_id="histtcb1")
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatView)
        bubbles = list(chat.query(ToolCallBubble))
        assert len(bubbles) == 1
        assert bubbles[0]._name == "web_fetch"
        assert bubbles[0]._args.get("url") == "https://www.mxhero.com"


@pytest.mark.asyncio
async def test_history_assistant_with_only_tool_calls_has_no_empty_bubble() -> None:
    """Empty-content assistant messages (just tool_calls) must NOT produce
    a stray empty MessageBubble — the tool result bubbles cover them."""
    messages = [
        {"role": "user", "content": "search"},
        {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "call_y1", "type": "function",
                "function": {"name": "web_search", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_y1", "name": "web_search",
         "content": "results: 0"},
        {"role": "assistant", "content": "No results."},
    ]
    loop = _fake_agent_loop(messages)
    app = DurinApp(agent_loop=loop, cli_chat_id="histtcb2")
    async with app.run_test() as pilot:
        await pilot.pause()
        chat = app.query_one(ChatView)
        assistant_bubbles = [
            b for b in chat.query(MessageBubble) if b._role == "assistant"
        ]
        # Only the final "No results." assistant message should appear.
        assert len(assistant_bubbles) == 1
        assert assistant_bubbles[0].body.strip() == "No results."


@pytest.mark.asyncio
async def test_history_replay_does_not_crash_on_orphan_tool_message() -> None:
    """A tool message whose tool_call_id has no matching assistant entry
    must NOT crash; fall back gracefully."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "missing", "name": "exec",
         "content": "some result"},
    ]
    loop = _fake_agent_loop(messages)
    app = DurinApp(agent_loop=loop, cli_chat_id="histtcb3")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Smoke test: the test passing without exception is the assertion.
        chat = app.query_one(ChatView)
        assert chat is not None
