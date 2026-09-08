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
        pinned_chars=1200,
        hot_chars=800,
        prefetch_hits=2,
    )

    assert rec.events == [
        (
            "turn.memory_usage",
            {
                "session_key": "websocket:c1",
                "search_calls": 2,
                "drill_calls": 1,
                "tool_calls_total": 4,
                "pinned_chars": 1200,
                "hot_chars": 800,
                "prefetch_hits": 2,
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
    await loop.close_mcp()  # drain background tasks before teardown

    rows = [d for t, d in rec.events if t == "turn.memory_usage"]
    assert len(rows) == 1, "exactly one turn.memory_usage row per turn"
    assert rows[0]["search_calls"] == 0
    assert rows[0]["drill_calls"] == 0
    assert rows[0]["tool_calls_total"] == 0
    assert rows[0]["session_key"].startswith("websocket:")
    assert isinstance(rows[0]["pinned_chars"], int)
    assert isinstance(rows[0]["hot_chars"], int)


@pytest.mark.asyncio
async def test_rollup_reports_non_zero_surface_sizes_when_memory_is_pinned(
    tmp_path: Path, monkeypatch,
) -> None:
    """With an always_on page on disk the prompt carries a pinned block and a
    hot layer (the Known types line at least), so both sizes must be real
    counts, not the (0, 0) fallback."""
    from datetime import datetime, timezone

    from durin.memory.field_patch import FieldPatch
    from durin.memory.memory_writer import write_entity
    from durin.memory.principal import mark_always_on

    now = datetime.now(timezone.utc)
    write_entity(tmp_path, "practice:spanish",
                 [FieldPatch(kind="body_append", value="Always respond in Spanish.",
                             author="agent", source_ref="s", at=now)],
                 create=True, name="Always Spanish")
    mark_always_on(tmp_path, "practice:spanish")

    rec = _capture(monkeypatch)
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model",
    )
    loop.provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="Hola.", tool_calls=[]),
    )
    loop.tools.get_definitions = MagicMock(return_value=[])

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content="hola"),
    )
    await loop.close_mcp()  # drain background tasks before teardown

    rows = [d for t, d in rec.events if t == "turn.memory_usage"]
    assert len(rows) == 1
    assert rows[0]["pinned_chars"] > 0
    assert rows[0]["hot_chars"] > 0
