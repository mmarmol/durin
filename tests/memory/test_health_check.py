"""Health-check cron.

Periodic probe of memory subsystem components:
- FTS5 index reachable.
- LanceDB connect (best-effort; optional dep).
- File-system staleness via `detect_index_staleness`.

Emits `memory.health_check` per tick with per-component status.
Three consecutive failures of the same component → `memory.health.critical`.

The cron itself uses a polling thread driven by a configurable
`interval_seconds` (default 900). Tests drive it manually via
`run_tick()` for determinism.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage
from durin.memory.indexer import rebuild_fts_index


def _seed(workspace: Path) -> None:
    EntityPage(
        type="person", name="Marcelo", aliases=["m"], body="b",
    ).save(workspace / "memory" / "entities" / "person" / "marcelo.md")
    rebuild_fts_index(workspace)


def test_clean_workspace_reports_all_ok(tmp_path: Path) -> None:
    from durin.memory.health_check import HealthChecker
    _seed(tmp_path)
    checker = HealthChecker(tmp_path)
    result = checker.run_tick()
    assert result["status"] == "ok"
    assert result["components"]["fts"] == "ok"
    # `lance` is "skipped" when the dep isn't installed/configured.
    assert result["components"]["lance"] in ("ok", "skipped")
    assert result["drift_count"] == 0


def test_drift_detected_triggers_repair(tmp_path: Path) -> None:
    """A `missing_row` drift triggers a re-index of the missing file."""
    from durin.memory.fts_index import FTSIndex
    from durin.memory.health_check import HealthChecker
    _seed(tmp_path)
    # Add a new entity directly (bypassing the tool re-index hook) so
    # the file has no FTS row → missing_row drift.
    EntityPage(
        type="person", name="New", aliases=[], body="drifted",
    ).save(tmp_path / "memory" / "entities" / "person" / "new.md")
    # Confirm drift exists before the tick.
    from durin.memory.indexer import detect_index_staleness
    issues = detect_index_staleness(tmp_path)
    assert any(i["uri"] == "person:new" for i in issues)
    checker = HealthChecker(tmp_path)
    result = checker.run_tick()
    assert result["drift_count"] >= 1
    # Post-tick: drift was repaired via reindex.
    with FTSIndex.open(tmp_path) as idx:
        hits = idx.search("drifted")
    assert any(h.uri == "person:new" for h in hits)


def test_emits_health_check_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from durin.memory.health_check import HealthChecker
    _seed(tmp_path)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.health_check.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    HealthChecker(tmp_path).run_tick()
    assert any(e[0] == "memory.health_check" for e in events)


def test_three_consecutive_fails_emit_critical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 consecutive failures of the same component within the
    escalation window emit `memory.health.critical`."""
    from durin.memory.health_check import HealthChecker

    _seed(tmp_path)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.health_check.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    # Force `fts` component to fail by patching the probe.
    monkeypatch.setattr(
        "durin.memory.health_check.HealthChecker._probe_fts",
        lambda self: ("fail", "simulated failure"),
    )
    checker = HealthChecker(tmp_path)
    for _ in range(3):
        checker.run_tick()
    critical = [e for e in events if e[0] == "memory.health.critical"]
    assert len(critical) >= 1
    assert critical[0][1].get("component") == "fts"


def test_success_resets_failure_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful tick after a failure resets the counter so a
    later transient failure doesn't immediately escalate."""
    from durin.memory.health_check import HealthChecker

    _seed(tmp_path)
    fail_mode = {"value": True}

    def maybe_fail(self) -> tuple:
        if fail_mode["value"]:
            return ("fail", "simulated")
        return ("ok", "")

    monkeypatch.setattr(
        "durin.memory.health_check.HealthChecker._probe_fts",
        maybe_fail,
    )
    checker = HealthChecker(tmp_path)
    checker.run_tick()  # fail
    fail_mode["value"] = False
    checker.run_tick()  # ok — should reset counter
    fail_mode["value"] = True
    checker.run_tick()  # fail #1 of a new streak

    assert checker.consecutive_failures("fts") == 1


def test_skips_lance_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If LanceDB isn't importable / present, the probe reports
    `skipped`, not `fail` — operational gap, not corruption."""
    from durin.memory.health_check import HealthChecker

    _seed(tmp_path)
    monkeypatch.setattr(
        "durin.memory.vector_index.vector_index_available",
        lambda: False,
    )
    result = HealthChecker(tmp_path).run_tick()
    assert result["components"]["lance"] == "skipped"


# ---------------------------------------------------------------------------
# P11 Fix B (2026-05-30): cross-encoder probe
# ---------------------------------------------------------------------------


def test_cross_encoder_probe_skipped_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CE disabled in config → probe reports `skipped`. Most users
    don't enable CE; we shouldn't pepper their tick with warns."""
    from durin.memory.health_check import HealthChecker

    _seed(tmp_path)

    class _FakeCE:
        enabled = False
        model = "x"

    class _FakeSearch:
        cross_encoder = _FakeCE()

    class _FakeMem:
        search = _FakeSearch()

    class _FakeCfg:
        memory = _FakeMem()

    monkeypatch.setattr(
        "durin.config.loader.load_config", lambda: _FakeCfg(),
    )
    result = HealthChecker(tmp_path).run_tick()
    assert result["components"]["cross_encoder"] == "skipped"


def test_cross_encoder_probe_ok_when_load_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CE enabled + working load → probe `ok`."""
    from durin.memory.health_check import HealthChecker

    _seed(tmp_path)

    class _FakeCE:
        enabled = True
        model = "fake/model"

    class _FakeSearch:
        cross_encoder = _FakeCE()

    class _FakeMem:
        search = _FakeSearch()

    class _FakeCfg:
        memory = _FakeMem()

    monkeypatch.setattr(
        "durin.config.loader.load_config", lambda: _FakeCfg(),
    )
    # Patch the loader so we don't actually pull the model
    monkeypatch.setattr(
        "durin.memory.cross_encoder._load_default_scorer",
        lambda model: type(
            "_S", (), {"score": lambda self, pairs: [0.5] * len(pairs)}
        )(),
    )
    result = HealthChecker(tmp_path).run_tick()
    assert result["components"]["cross_encoder"] == "ok"


def test_cross_encoder_probe_fail_when_load_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CE enabled + load returns None (sentence_transformers missing
    OR model unreachable) → probe `fail`. The 3-strike escalation
    fires after 3 consecutive failed ticks."""
    from durin.memory.health_check import HealthChecker

    _seed(tmp_path)

    class _FakeCE:
        enabled = True
        model = "fake/model"

    class _FakeSearch:
        cross_encoder = _FakeCE()

    class _FakeMem:
        search = _FakeSearch()

    class _FakeCfg:
        memory = _FakeMem()

    monkeypatch.setattr(
        "durin.config.loader.load_config", lambda: _FakeCfg(),
    )
    monkeypatch.setattr(
        "durin.memory.cross_encoder._load_default_scorer",
        lambda model: None,  # simulate failed load
    )
    checker = HealthChecker(tmp_path)
    result = checker.run_tick()
    assert result["components"]["cross_encoder"] == "fail"
    assert "cross-encoder" in result["errors"]["cross_encoder"].lower()
