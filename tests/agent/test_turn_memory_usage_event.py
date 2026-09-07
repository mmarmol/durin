"""Tests for the per-turn ``turn.memory_usage`` telemetry rollup.

Emitted once per turn at save time so silent-miss and prefetch
substitution analysis can read turn-level memory-recall activity
without reconstructing turn boundaries from the raw event stream.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop, emit_memory_usage_rollup
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.providers.base import LLMResponse


class _Rec:
    """Session-logger stand-in that records every .log() call."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type, data=None):
        self.events.append((event_type, dict(data or {})))


def _capture(monkeypatch) -> _Rec:
    rec = _Rec()
    monkeypatch.setattr(
        "durin.telemetry.logger.get_session_logger",
        lambda key, base_dir=None: rec,
    )
    return rec


def test_rollup_counts_memory_recall_tools(monkeypatch):
    rec = _capture(monkeypatch)

    emit_memory_usage_rollup(
        "websocket:c1",
        ["memory_search", "read_file", "memory_search", "memory_drill"],
    )

    assert rec.events == [
        (
            "turn.memory_usage",
            {
                "session_key": "websocket:c1",
                "search_calls": 2,
                "drill_calls": 1,
                "tool_calls_total": 4,
            },
        )
    ]


def test_rollup_emits_zero_row_for_turn_without_tools(monkeypatch):
    """Turns that never touched memory must still emit — the
    ``search_calls == 0`` rows are the silent-miss signal."""
    rec = _capture(monkeypatch)

    emit_memory_usage_rollup("websocket:c1", [])

    assert len(rec.events) == 1
    event_type, data = rec.events[0]
    assert event_type == "turn.memory_usage"
    assert data["search_calls"] == 0
    assert data["drill_calls"] == 0
    assert data["tool_calls_total"] == 0


def test_rollup_swallows_logger_failures(monkeypatch):
    def _boom(key, base_dir=None):
        raise OSError("disk full")

    monkeypatch.setattr("durin.telemetry.logger.get_session_logger", _boom)

    emit_memory_usage_rollup("websocket:c1", ["memory_search"])  # must not raise


@pytest.mark.asyncio
async def test_rollup_reaches_the_session_logger_from_a_real_turn(
    tmp_path: Path, monkeypatch,
) -> None:
    """Regression: the save state runs after ``_run_agent_loop`` has reset
    the telemetry contextvar, so an emit routed through
    ``current_telemetry()`` is silently dropped. Drive a whole turn and
    read what the session logger actually received."""
    rec = _capture(monkeypatch)
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model",
    )
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="Hi there.", tool_calls=[]),
    )
    loop.tools.get_definitions = MagicMock(return_value=[])

    msg = InboundMessage(channel="websocket", sender_id="u", chat_id="c", content="hi")
    await loop._process_message(msg)

    rows = [d for t, d in rec.events if t == "turn.memory_usage"]
    assert len(rows) == 1, "exactly one turn.memory_usage row per turn"
    assert rows[0]["search_calls"] == 0
    assert rows[0]["drill_calls"] == 0
    assert rows[0]["tool_calls_total"] == 0
    assert rows[0]["session_key"].startswith("websocket:")
