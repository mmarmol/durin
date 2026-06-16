"""F20 (audit third pass, 2026-05-28): doc 07 §4.1 promised that
`iteration` and `session_key` are "auto-injected by emit_tool_event".

Pre-F20 the claim was aspirational: the TypedDicts declared both
fields as ``NotRequired`` and no emit site ever populated them.
Dashboards joining `memory.recall` to other events on
`(session_key, iteration)` had nothing to join on.

F20 wires the producer:
- `TelemetryLogger` carries a `session_key` and a per-turn
  `iteration` counter.
- `get_session_logger(session_key, ...)` stamps the key.
- `AgentLoop`'s existing `on_iteration` callback feeds the counter
  into the bound logger.
- `emit_tool_event` auto-injects both into the event payload
  (caller-supplied values win — overrides are explicit).
"""

from __future__ import annotations

from pathlib import Path


def test_session_logger_stamps_session_key(tmp_path: Path) -> None:
    from durin.telemetry.logger import get_session_logger

    logger = get_session_logger("telegram:c1", base_dir=tmp_path)
    assert logger.session_key == "telegram:c1"


def test_logger_iteration_starts_at_zero(tmp_path: Path) -> None:
    from durin.telemetry.logger import get_session_logger

    logger = get_session_logger("telegram:c1", base_dir=tmp_path)
    assert logger.iteration == 0


def test_logger_set_iteration_updates_counter(tmp_path: Path) -> None:
    from durin.telemetry.logger import get_session_logger

    logger = get_session_logger("telegram:c1", base_dir=tmp_path)
    logger.set_iteration(3)
    assert logger.iteration == 3


def test_emit_tool_event_auto_injects_session_and_iteration(
    tmp_path: Path,
) -> None:
    """When no `session_key`/`iteration` is in the payload, the
    helper fills them from the bound TelemetryLogger."""
    from durin.agent.tools._telemetry import emit_tool_event
    from durin.telemetry.logger import bind_telemetry, get_session_logger

    logger = get_session_logger("telegram:c1", base_dir=tmp_path)
    logger.set_iteration(5)
    token = bind_telemetry(logger)
    try:
        emit_tool_event("memory.recall", {"query": "x", "scope": "all"})
    finally:
        from durin.telemetry.logger import reset_telemetry
        reset_telemetry(token)

    # Read back the JSONL line.
    import json
    lines = logger.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "memory.recall"
    assert entry["data"]["session_key"] == "telegram:c1"
    assert entry["data"]["iteration"] == 5


def test_caller_supplied_values_win(tmp_path: Path) -> None:
    """Explicit `session_key`/`iteration` in the payload override
    the auto-injection — lets callers stamp a different identity
    when needed (e.g. subagent forwarding parent's session)."""
    from durin.agent.tools._telemetry import emit_tool_event
    from durin.telemetry.logger import (
        bind_telemetry,
        get_session_logger,
        reset_telemetry,
    )

    logger = get_session_logger("telegram:c1", base_dir=tmp_path)
    logger.set_iteration(5)
    token = bind_telemetry(logger)
    try:
        emit_tool_event(
            "memory.recall",
            {
                "query": "x",
                "scope": "all",
                "session_key": "cli:headless",
                "iteration": 99,
            },
        )
    finally:
        reset_telemetry(token)

    import json
    entry = json.loads(logger.path.read_text(encoding="utf-8").strip())
    assert entry["data"]["session_key"] == "cli:headless"
    assert entry["data"]["iteration"] == 99


def test_no_logger_bound_does_not_crash(tmp_path: Path) -> None:
    """When no logger is bound (rare — script context), emit is a
    no-op rather than a crash."""
    from durin.agent.tools._telemetry import emit_tool_event

    # No bind_telemetry call → current_telemetry() returns None →
    # emit_tool_event silently returns.
    emit_tool_event("memory.recall", {"query": "x"})
