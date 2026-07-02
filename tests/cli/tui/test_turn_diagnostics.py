"""Live turn diagnostics + workflow needs_input continuity in the TUI.

Covers the two additions:
- `_retry_wait` outbound messages feed the footer retry badge (instead of
  being silently dropped) and clear when content flows again;
- a terminal `workflow_progress` frame with status=needs_input raises a
  system note in chat so the user can answer there (the agent owns resume).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from durin.bus.events import OutboundMessage
from durin.bus.queue import MessageBus
from durin.cli.tui.app import DurinApp
from durin.cli.tui.widgets import ChatView


def _fake_agent_loop(bus: MessageBus, tmp_path) -> SimpleNamespace:
    async def _idle_run() -> None:
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
        OutboundMessage(channel="cli", chat_id="direct", content=content, metadata=metadata)
    )


@pytest.mark.asyncio
async def test_retry_wait_sets_and_clears_footer_status(tmp_path) -> None:
    bus = MessageBus()
    app = DurinApp(agent_loop=_fake_agent_loop(bus, tmp_path))
    async with app.run_test() as pilot:
        status = {"kind": "retry_wait", "attempt": 2, "max_attempts": 10,
                  "delay_s": 14, "persistent": False, "final": False}
        await _inject(bus, "retrying…", _retry_wait=True, retry_status=status)
        await pilot.pause(0.1)
        assert app._retry_status == status
        payload = app._footer_payload()
        assert payload is not None and payload["retry_status"] == status

        # Content flowing again means the provider call succeeded — badge gone.
        chat = app.query_one(ChatView)
        app._current_assistant_bubble = chat.add_message("assistant", "")
        await _inject(bus, "Hel", _stream_delta=True)
        await pilot.pause(0.1)
        assert app._retry_status is None


@pytest.mark.asyncio
async def test_turn_end_clears_diagnostics(tmp_path) -> None:
    bus = MessageBus()
    app = DurinApp(agent_loop=_fake_agent_loop(bus, tmp_path))
    async with app.run_test() as pilot:
        app._turn_started_at = 12345.0
        app._retry_status = {"kind": "retry_wait", "attempt": 1, "max_attempts": 3,
                             "delay_s": 1, "persistent": False, "final": False}
        await _inject(bus, "", _streamed=True)
        await pilot.pause(0.1)
        assert app._turn_started_at is None
        assert app._retry_status is None
        assert app._last_latency_ms is not None


@pytest.mark.asyncio
async def test_workflow_needs_input_raises_system_note(tmp_path) -> None:
    bus = MessageBus()
    app = DurinApp(agent_loop=_fake_agent_loop(bus, tmp_path))
    async with app.run_test() as pilot:
        ev = {
            "version": 1,
            "phase": "end",
            "call_id": "workflow:r1",
            "name": "workflow_progress",
            "status": "needs_input",
            "detail": "Which environment: staging or prod?",
            "arguments": {"workflow": "deploy", "task": "ship it"},
            "nodes": [],
        }
        await _inject(bus, "", _progress=True, _tool_hint=True, _tool_events=[ev])
        await pilot.pause(0.1)
        chat = app.query_one(ChatView)
        bodies = [b.body for b in chat.query("MessageBubble")]
        note = next((b for b in bodies if "needs your input" in (b or "")), None)
        assert note is not None
        assert "deploy" in note
        assert "Which environment" in note


@pytest.mark.asyncio
async def test_workflow_completed_end_raises_no_note(tmp_path) -> None:
    bus = MessageBus()
    app = DurinApp(agent_loop=_fake_agent_loop(bus, tmp_path))
    async with app.run_test() as pilot:
        ev = {
            "version": 1,
            "phase": "end",
            "call_id": "workflow:r2",
            "name": "workflow_progress",
            "status": "completed",
            "arguments": {"workflow": "deploy", "task": "ship it"},
            "nodes": [],
        }
        await _inject(bus, "", _progress=True, _tool_hint=True, _tool_events=[ev])
        await pilot.pause(0.1)
        chat = app.query_one(ChatView)
        bodies = [b.body for b in chat.query("MessageBubble")]
        assert not any("needs your input" in (b or "") for b in bodies)
