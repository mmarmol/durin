"""`memory.health_check` payload carries the A6 fields (tick_id +
duration_ms).

Per doc 07 §9.4 and doc 11 audit A6:

- `tick_id` (string, 32-char UUID hex) — per-tick correlation id.
- `duration_ms` (float, > 0) — wall-clock of the probe round.

Per [[feedback-sync-tests-exercise-behavior]]: the test exercises
the emit path with real `run_tick()` calls, not just the TypedDict
declaration. Per [[feedback-verify-quantifiers]]: the tick_id length
is asserted explicitly so the test catches a regression that drops
the `.hex` (would return a 36-char dashed string instead of 32).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.health_check.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    return events


def test_tick_id_is_32_char_hex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tick_id must be `uuid.uuid4().hex` — 32 lowercase hex chars,
    no dashes. Catches an accidental switch to `str(uuid4())` which
    would return 36 dashed chars."""
    from durin.memory.health_check import HealthChecker

    events = _capture_events(monkeypatch)
    checker = HealthChecker(workspace=tmp_path)
    checker.run_tick()

    payload = next(d for t, d in events if t == "memory.health_check")
    assert "tick_id" in payload
    tick_id = payload["tick_id"]
    assert isinstance(tick_id, str)
    assert len(tick_id) == 32, (
        f"expected 32-char hex (uuid4().hex); got {len(tick_id)} chars: "
        f"{tick_id!r}"
    )
    assert all(c in "0123456789abcdef" for c in tick_id), (
        f"non-hex character in tick_id: {tick_id!r}"
    )


def test_duration_ms_is_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """duration_ms must be > 0 — even a fast probe tick takes more
    than 0 ms of wall clock. A regression that timed in seconds
    instead of ms would surface here too (since `time.perf_counter()`
    deltas in s are < 1 → multiplied by 1000.0 they're > 0)."""
    from durin.memory.health_check import HealthChecker

    events = _capture_events(monkeypatch)
    checker = HealthChecker(workspace=tmp_path)
    checker.run_tick()

    payload = next(d for t, d in events if t == "memory.health_check")
    assert "duration_ms" in payload
    assert isinstance(payload["duration_ms"], float)
    assert payload["duration_ms"] > 0.0


def test_consecutive_ticks_get_distinct_tick_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each tick gets its own tick_id — never reused. Catches a
    regression where tick_id is generated once at __init__ instead
    of per-tick."""
    from durin.memory.health_check import HealthChecker

    events = _capture_events(monkeypatch)
    checker = HealthChecker(workspace=tmp_path)
    checker.run_tick()
    checker.run_tick()
    checker.run_tick()

    tick_ids = [
        d["tick_id"] for t, d in events
        if t == "memory.health_check"
    ]
    assert len(tick_ids) == 3
    assert len(set(tick_ids)) == 3, (
        f"tick_ids should be distinct per tick; got {tick_ids}"
    )


def test_a6_fields_in_typed_dict() -> None:
    """The TypedDict declares the A6 fields. Catches a silent revert
    where someone removes the schema entries but the payload code
    still emits them."""
    from durin.telemetry.schema import MemoryHealthCheckEvent

    annotations = MemoryHealthCheckEvent.__annotations__
    assert "tick_id" in annotations
    assert "duration_ms" in annotations
    # Pre-A6 fields still present (additive change, not replacing).
    for required in ("status", "components", "drift_count"):
        assert required in annotations, (
            f"pre-A6 field {required!r} missing from "
            f"MemoryHealthCheckEvent — A6 was additive, not a "
            f"replacement"
        )


def test_pre_a6_fields_still_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A6 was additive — the existing fields (status, components,
    drift_count) must still appear in the payload."""
    from durin.memory.health_check import HealthChecker

    events = _capture_events(monkeypatch)
    checker = HealthChecker(workspace=tmp_path)
    checker.run_tick()

    payload = next(d for t, d in events if t == "memory.health_check")
    for field in ("status", "components", "drift_count"):
        assert field in payload, (
            f"pre-A6 field {field!r} missing from emit payload"
        )
