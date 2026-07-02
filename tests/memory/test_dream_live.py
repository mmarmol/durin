"""Live dream tee + telemetry-persistence regression tests.

Covers the fix for "the cron dream emitted nothing, so the digest was always
empty after a run": the dream now binds a TelemetryLogger (so its events persist
where the digest reads) and tees each activity item to a publish callback (the
live websocket feed). One emit feeds both surfaces.
"""

from __future__ import annotations

from pathlib import Path

from durin.memory.dream_live import DreamProgressSink


def test_sink_forwards_activity_items() -> None:
    captured: list[dict] = []
    sink = DreamProgressSink(captured.append)
    sink.log("memory.absorb.auto_merged", {
        "canonical": "place:torrent", "absorbed": "place:torrent-valencia",
    })
    assert len(captured) == 1
    assert captured[0]["kind"] == "activity"
    item = captured[0]["item"]
    assert item["kind"] == "merged"
    assert item["ref"] == "place:torrent"
    assert item["ref_kind"] == "entity"


def test_sink_expands_multi_ref_events() -> None:
    captured: list[dict] = []
    sink = DreamProgressSink(captured.append)
    sink.log("memory.dream.discover", {"refs": ["person:ana", "org:acme"]})
    assert [c["item"]["ref"] for c in captured] == ["person:ana", "org:acme"]
    assert all(c["kind"] == "activity" for c in captured)


def test_sink_suppresses_zero_count_noise() -> None:
    """A dream run that changed nothing should produce no feed items. The
    skill-extract pass fires on every run, so a `skills_touched=0` item (and the
    other zero-count passes) would be per-run noise in the live feed / digest.
    """
    captured: list[dict] = []
    sink = DreamProgressSink(captured.append)
    sink.log("memory.dream.skill_extract", {"skills_touched": 0})
    sink.log("memory.dream.discover", {"refs": [], "written": 0})
    sink.log("memory.dream.learnings", {"refs": [], "written": 0})
    sink.log("memory.dream.skill_signals", {"skills": [], "logged": 0})
    assert captured == []


def test_run_summary_empty_run_still_produces_an_entry() -> None:
    """A run that changed nothing must still leave a "ran, nothing new" entry.
    This is the basic expectation: every run is visible, even a no-op run —
    otherwise an empty run silently shows nothing and looks broken.
    """
    from durin.memory.dream_digest import map_dream_event

    items = map_dream_event(
        "memory.dream.run_summary",
        {"sessions": 0, "entities": 0, "merged": 0, "skills_created": 0, "skills_improved": 0}, 1)
    assert len(items) == 1
    assert items[0]["kind"] == "run"
    assert items[0]["summary"] == "Dream run — no new changes"


def test_run_summary_with_changes_lists_them() -> None:
    from durin.memory.dream_digest import map_dream_event

    items = map_dream_event(
        "memory.dream.run_summary",
        {"sessions": 4, "entities": 2, "merged": 1,
         "skills_created": 3, "skills_improved": 1}, 1)
    assert items[0]["kind"] == "run"
    assert "1 merge(s)" in items[0]["summary"]
    assert "2 entity update(s)" in items[0]["summary"]
    assert "3 new skill(s)" in items[0]["summary"]
    assert "1 skill edit(s)" in items[0]["summary"]


def test_sink_forwards_run_summary_live() -> None:
    captured: list[dict] = []
    sink = DreamProgressSink(captured.append)
    sink.log("memory.dream.run_summary",
             {"sessions": 0, "entities": 0, "merged": 0, "skills_created": 0, "skills_improved": 0})
    assert len(captured) == 1
    assert captured[0]["item"]["kind"] == "run"


def test_sink_ignores_run_markers_and_non_dream_events() -> None:
    captured: list[dict] = []
    sink = DreamProgressSink(captured.append)
    sink.log("memory.dream.start", {})
    sink.log("memory.dream.end", {"kind": "extract"})
    sink.log("provider.rate_limit", {"attempt": 1})
    sink.log("some.unrelated.event", {"x": 1})
    assert captured == []


def test_bound_logger_persists_dream_events_for_digest(
    tmp_path: Path, monkeypatch
) -> None:
    """The core regression: with a bound telemetry logger, emit_tool_event
    writes to the telemetry dir and the dream digest reflects the run.

    Before the fix the cron dream bound no logger, so emit_tool_event was a
    silent no-op and the digest stayed empty even after a real run.
    """
    from durin.agent.tools._telemetry import emit_tool_event
    from durin.service.memory import _build_dream_digest
    from durin.telemetry.logger import (
        bind_telemetry,
        get_session_logger,
        reset_telemetry,
    )

    tel_dir = tmp_path / "telemetry"
    tok = bind_telemetry(get_session_logger("cron_dream", base_dir=tel_dir))
    try:
        emit_tool_event("memory.absorb.auto_merged", {
            "canonical": "place:torrent", "absorbed": "place:torrent-valencia",
        })
    finally:
        reset_telemetry(tok)

    monkeypatch.setattr("durin.service.memory._telemetry_dir", lambda: tel_dir)
    digest = _build_dream_digest(30)

    assert len(digest.events) == 1
    assert digest.events[0].kind == "merged"
    assert digest.events[0].ref == "place:torrent"


def test_no_bound_logger_persists_nothing(tmp_path: Path, monkeypatch) -> None:
    """Sanity guard on the bug itself: without a bound logger emit is a no-op,
    so the digest sees nothing — the exact symptom the fix removes.
    """
    from durin.agent.tools._telemetry import emit_tool_event
    from durin.service.memory import _build_dream_digest

    tel_dir = tmp_path / "telemetry"
    emit_tool_event("memory.absorb.auto_merged", {
        "canonical": "place:torrent", "absorbed": "place:torrent-valencia",
    })
    monkeypatch.setattr("durin.service.memory._telemetry_dir", lambda: tel_dir)
    assert _build_dream_digest(30).events == []


def test_logger_sink_tee_fires_alongside_persistence(tmp_path: Path) -> None:
    """A DreamProgressSink registered on the logger tees each event live while
    the JSONL persistence (for the digest) still happens — one emit, both
    surfaces, in lockstep.
    """
    from durin.agent.tools._telemetry import emit_tool_event
    from durin.telemetry.logger import (
        bind_telemetry,
        get_session_logger,
        reset_telemetry,
    )

    tel_dir = tmp_path / "telemetry"
    logger = get_session_logger("cron_dream", base_dir=tel_dir)
    captured: list[dict] = []
    logger.add_sink(DreamProgressSink(captured.append))
    tok = bind_telemetry(logger)
    try:
        emit_tool_event("memory.dream.flagged", {
            "canonical": "person:alice", "absorbed": "person:alice-v2",
        })
    finally:
        reset_telemetry(tok)

    # live tee fired
    assert len(captured) == 1
    assert captured[0]["item"]["kind"] == "flagged"
    # persistence happened too (file on disk for the digest to read later)
    files = list(tel_dir.glob("*.jsonl"))
    assert len(files) == 1
    assert "memory.dream.flagged" in files[0].read_text(encoding="utf-8")


def test_parse_failure_maps_to_warning_with_entity_deep_link() -> None:
    from durin.memory.dream_digest import map_dream_event

    items = map_dream_event(
        "memory.dream.parse_failure",
        {"stage": "extract", "source": "person:ana", "raw_head": "garbage"}, 5)
    assert len(items) == 1
    assert items[0]["kind"] == "warning"
    assert "extract" in items[0]["summary"]
    assert items[0]["ref"] == "person:ana"
    assert items[0]["ref_kind"] == "entity"


def test_parse_failure_without_entity_source_has_no_ref() -> None:
    from durin.memory.dream_digest import map_dream_event

    items = map_dream_event(
        "memory.dream.parse_failure",
        {"stage": "derived_from", "source": "session-stem", "raw_head": "x"}, 5)
    assert len(items) == 1
    assert items[0]["kind"] == "warning"
    assert items[0]["ref"] is None
    assert items[0]["ref_kind"] is None


def test_sink_forwards_parse_failure_items() -> None:
    captured: list[dict] = []
    sink = DreamProgressSink(captured.append)
    sink.log("memory.dream.parse_failure",
             {"stage": "learnings", "source": None, "raw_head": "??"})
    assert len(captured) == 1
    assert captured[0]["item"]["kind"] == "warning"


def test_vector_unavailable_maps_to_warning() -> None:
    from durin.memory.dream_digest import map_dream_event

    items = map_dream_event("memory.dream.vector_unavailable", {}, 5)
    assert len(items) == 1
    assert items[0]["kind"] == "warning"
    assert items[0]["ref"] is None


def test_dream_vector_index_emits_when_enabled_but_unavailable(monkeypatch, tmp_path) -> None:
    """memory.enabled=true but lancedb missing -> the run degrades semantic
    dedup silently; that must emit memory.dream.vector_unavailable."""
    import durin.agent.tools._telemetry as tel
    import durin.memory.dream_passes as dp

    events: list[tuple] = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    monkeypatch.setattr(dp, "vector_index_available", lambda: False)

    class _Cfg:
        class memory:
            enabled = True

    assert dp.dream_vector_index(tmp_path, _Cfg) is None
    assert [n for n, _ in events] == ["memory.dream.vector_unavailable"]


def test_dream_vector_index_silent_when_memory_disabled(monkeypatch, tmp_path) -> None:
    """Vectors deliberately off (memory.enabled=false) is expected degradation,
    not a warning."""
    import durin.agent.tools._telemetry as tel
    import durin.memory.dream_passes as dp

    events: list[tuple] = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    monkeypatch.setattr(dp, "vector_index_available", lambda: False)

    class _Cfg:
        class memory:
            enabled = False

    assert dp.dream_vector_index(tmp_path, _Cfg) is None
    assert events == []
