"""Tests for the per-turn ``turn.memory_usage`` telemetry rollup.

Emitted once per turn at save time so silent-miss and prefetch
substitution analysis can read turn-level memory-recall activity
without reconstructing turn boundaries from the raw event stream.
"""

from __future__ import annotations

from durin.agent.loop import emit_memory_usage_rollup


class _CapturingLogger:
    """In-memory TelemetryLogger drop-in that records every .log() call."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.session_key = "sess-1"
        self.iteration = 4

    def log(self, event_type, data=None):
        self.events.append((event_type, dict(data or {})))


def _bind_telemetry(monkeypatch) -> list[tuple[str, dict]]:
    logger = _CapturingLogger()
    monkeypatch.setattr(
        "durin.agent.tools._telemetry.current_telemetry", lambda: logger
    )
    return logger.events


def test_rollup_counts_memory_recall_tools(monkeypatch):
    events = _bind_telemetry(monkeypatch)

    emit_memory_usage_rollup(
        ["memory_search", "read_file", "memory_search", "memory_drill"]
    )

    assert events == [
        (
            "turn.memory_usage",
            {
                "search_calls": 2,
                "drill_calls": 1,
                "tool_calls_total": 4,
                "session_key": "sess-1",
                "iteration": 4,
            },
        )
    ]


def test_rollup_emits_zero_row_for_turn_without_tools(monkeypatch):
    """Turns that never touched memory must still emit — the
    ``search_calls == 0`` rows are the silent-miss signal."""
    events = _bind_telemetry(monkeypatch)

    emit_memory_usage_rollup([])

    assert len(events) == 1
    event_type, data = events[0]
    assert event_type == "turn.memory_usage"
    assert data["search_calls"] == 0
    assert data["drill_calls"] == 0
    assert data["tool_calls_total"] == 0


def test_rollup_is_noop_without_bound_telemetry(monkeypatch):
    monkeypatch.setattr(
        "durin.agent.tools._telemetry.current_telemetry", lambda: None
    )

    emit_memory_usage_rollup(["memory_search"])
