"""D5.3 — streaming + bus integration tests.

Uses a real MessageBus but no LLM. The test pre-populates the bus's
outbound queue (or publishes after the app has mounted) so the
consumer worker exercises every metadata-flag path that mirrors
durin/cli/commands.py:_consume_outbound.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from durin.bus.events import InboundMessage, OutboundMessage
from durin.bus.queue import MessageBus
from durin.cli.tui.app import DurinApp
from durin.cli.tui.widgets import ChatView, MessageBubble


def _fake_agent_loop(bus: MessageBus, tmp_path) -> SimpleNamespace:
    """Return an agent_loop stand-in that owns the bus but never dispatches."""

    async def _idle_run() -> None:
        # The real AgentLoop.run drives the bus; we just sit idle so the
        # outbound consumer can run while we inject messages manually.
        await asyncio.Event().wait()

    return SimpleNamespace(
        bus=bus,
        workspace=str(tmp_path),
        model="test-model",
        model_preset="default",
        context_window_tokens=200_000,
        sessions=SimpleNamespace(
            get_or_create=lambda key: SimpleNamespace(messages=[], metadata={})
        ),
        run=_idle_run,
    )


async def _inject(bus: MessageBus, content: str, **metadata: Any) -> None:
    await bus.publish_outbound(
        OutboundMessage(
            channel="cli",
            chat_id="direct",
            content=content,
            metadata=metadata,
        )
    )


# ---------------------------------------------------------------------------
# Streaming deltas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_delta_appends_to_open_assistant_bubble(tmp_path) -> None:
    bus = MessageBus()
    app = DurinApp(agent_loop=_fake_agent_loop(bus, tmp_path))
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        # Simulate the user submitting; the app opens an assistant bubble.
        app._current_assistant_bubble = chat.add_message("assistant", "")
        await _inject(bus, "Hel", _stream_delta=True)
        await _inject(bus, "lo, ", _stream_delta=True)
        await _inject(bus, "world", _stream_delta=True)
        await _inject(bus, "", _stream_end=True)
        await pilot.pause()
        # Stream end clears the cursor; deltas accumulated in the bubble.
        bubbles = list(chat.query(MessageBubble))
        assert bubbles[-1]._role == "assistant"
        assert bubbles[-1].body == "Hello, world"
        assert app._current_assistant_bubble is None


@pytest.mark.asyncio
async def test_streamed_marker_does_not_render(tmp_path) -> None:
    bus = MessageBus()
    app = DurinApp(agent_loop=_fake_agent_loop(bus, tmp_path))
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        baseline = len(list(chat.query(MessageBubble)))
        await _inject(bus, "ignore me", _streamed=True)
        await pilot.pause()
        assert len(list(chat.query(MessageBubble))) == baseline


@pytest.mark.asyncio
async def test_non_stream_content_creates_assistant_bubble(tmp_path) -> None:
    bus = MessageBus()
    app = DurinApp(agent_loop=_fake_agent_loop(bus, tmp_path))
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        await _inject(bus, "complete response")
        await pilot.pause()
        bubbles = list(chat.query(MessageBubble))
        assert any(b._role == "assistant" and b.body == "complete response" for b in bubbles)


@pytest.mark.asyncio
async def test_non_stream_content_with_render_as_text_renders_as_system(tmp_path) -> None:
    bus = MessageBus()
    app = DurinApp(agent_loop=_fake_agent_loop(bus, tmp_path))
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        await _inject(bus, "slash command response", render_as="text")
        await pilot.pause()
        bubbles = list(chat.query(MessageBubble))
        assert bubbles[-1]._role == "system"


# ---------------------------------------------------------------------------
# Session switching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_chat_id_mutates_cli_chat_id(tmp_path) -> None:
    bus = MessageBus()
    app = DurinApp(agent_loop=_fake_agent_loop(bus, tmp_path))
    async with app.run_test() as pilot:
        assert app._cli_chat_id == "direct"
        await _inject(bus, "Switched to session `cli:project-b`.", _switch_chat_id="project-b")
        await pilot.pause()
        assert app._cli_chat_id == "project-b"


@pytest.mark.asyncio
async def test_user_submission_publishes_inbound(tmp_path) -> None:
    bus = MessageBus()
    app = DurinApp(agent_loop=_fake_agent_loop(bus, tmp_path))
    received: list[InboundMessage] = []

    async def _drain():
        while True:
            try:
                msg = await asyncio.wait_for(bus.consume_inbound(), timeout=0.5)
            except asyncio.TimeoutError:
                return
            received.append(msg)

    async with app.run_test() as pilot:
        inp = app.query_one("InputArea")
        inp.focus()
        await pilot.pause()
        inp.value = "hola"
        await pilot.press("enter")
        await pilot.pause()
        # Wait briefly for the inbound publish to flush.
        await _drain()

    assert any(m.content == "hola" and m.chat_id == "direct" for m in received)


@pytest.mark.asyncio
async def test_blocking_ask_user_does_not_duplicate_in_tui(tmp_path) -> None:
    """E2E render check: a blocking ask_user's end frame must update the
    SAME bubble created at start — even when the user's answer bubble was
    mounted in between — so the question never duplicates (the webui's
    dup bug does not exist here, keyed by call_id)."""
    from durin.cli.tui.widgets import ToolCallBubble

    bus = MessageBus()
    app = DurinApp(agent_loop=_fake_agent_loop(bus, tmp_path))
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        # 1. start frame → question bubble
        await _inject(bus, "ask_user_question(...)", _tool_hint=True, _tool_events=[
            {"version": 1, "phase": "start", "call_id": "q1",
             "name": "ask_user_question",
             "arguments": {"question": "¿Qué color?", "options": ["Rojo", "Verde"]}},
        ])
        await pilot.pause()
        # 2. the user answers — a user bubble lands between start and end
        chat.add_message("user", "Rojo")
        await pilot.pause()
        # 3. end frame for the SAME call_id
        await _inject(bus, "", _tool_hint=True, _tool_events=[
            {"version": 1, "phase": "end", "call_id": "q1",
             "name": "ask_user_question", "result": "ok"},
        ])
        await pilot.pause()

        bubbles = list(chat.query(ToolCallBubble))
        # Exactly one tool bubble for q1 — no duplicate question.
        assert len(bubbles) == 1
        from tests.cli.tui.test_tool_call_bubble import _body_plain
        body = _body_plain(bubbles[0])
        assert "¿Qué color?" in body
        # A single ❓ in the rendered question.
        assert body.count("❓") == 1
