"""Self-heal: the health-check prunes orphan index rows for files that
were deleted out-of-band (e.g. a raw `rm`), which the file-watcher can't
see and which `forget` would otherwise be the only cleaner of.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from durin.memory.fts_index import FTSIndex
from durin.memory.health_check import HealthChecker
from durin.memory.indexer import reindex_one_file
from durin.memory.provenance import author_scope
from durin.memory.store import store_memory


def _indexed_stable(ws: Path) -> tuple[Path, str]:
    with author_scope("agent_created"):
        res = store_memory(
            ws, content="x", class_name="stable",
            entities=["company:mxhero"],
            valid_from=datetime.date(2026, 6, 4),
        )
    path = ws / "memory" / "stable" / f"{res['id']}.md"
    reindex_one_file(ws, path, trigger="test")
    return path, f"memory/stable/{res['id']}"


def _fts_uris(ws: Path) -> set[str]:
    with FTSIndex.open(ws) as idx:
        return {u for u, _ in idx.known_uris()}


def test_tick_prunes_orphan_fts_row_after_rm(tmp_path: Path) -> None:
    path, uri = _indexed_stable(tmp_path)
    assert uri in _fts_uris(tmp_path)
    path.unlink()  # out-of-band deletion — the bug scenario
    HealthChecker(tmp_path).run_tick()
    assert uri not in _fts_uris(tmp_path)


def test_tick_keeps_present_entry(tmp_path: Path) -> None:
    """A present, correctly-indexed entry must NOT be pruned (no false
    drift after the _uri_for fix)."""
    _path, uri = _indexed_stable(tmp_path)
    HealthChecker(tmp_path).run_tick()
    assert uri in _fts_uris(tmp_path)


def test_prune_orphans_direct(tmp_path: Path) -> None:
    path, uri = _indexed_stable(tmp_path)
    path.unlink()
    HealthChecker(tmp_path)._prune_orphans([uri])
    assert uri not in _fts_uris(tmp_path)


def test_vector_id_for_mapping() -> None:
    f = HealthChecker._vector_id_for
    assert f("memory/stable/abc123") == "abc123"
    assert f("person:marcelo") == "person:marcelo"
    assert f("skill/web-scraping") == "skill/web-scraping"


def _reference(ws: Path, slug: str) -> Path:
    path = ws / "memory" / "references" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {slug.title()}\n\ncuerpo del documento {slug}\n", encoding="utf-8")
    return path


def test_tick_indexes_an_unindexed_reference(tmp_path: Path) -> None:
    """The drift repair backfills a reference file with no FTS row —
    the box's vault-synced library docs, which no ingest call indexed."""
    _reference(tmp_path, "forja")
    assert "reference:forja" not in _fts_uris(tmp_path)
    HealthChecker(tmp_path).run_tick()
    assert "reference:forja" in _fts_uris(tmp_path)
    # And it does not re-flag on the next tick (uri now consistent).
    from durin.memory.indexer import detect_index_staleness
    assert not any(
        i["uri"] == "reference:forja" for i in detect_index_staleness(tmp_path)
    )


def _session(ws: Path, key: str, turns: int) -> Path:
    sessions = ws / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"## turn-{i}\n\nuser: mensaje {i}\n" for i in range(1, turns + 1))
    path = sessions / f"{key}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_tick_indexes_an_unindexed_session(tmp_path: Path) -> None:
    """The drift repair backfills a session file with no turn rows —
    sessions written before the incremental indexer, or on an install
    whose full rebuild never ran."""
    _session(tmp_path, "ws_a", turns=2)
    assert not any(u.startswith("sessions/ws_a.md#") for u in _fts_uris(tmp_path))
    HealthChecker(tmp_path).run_tick()
    turn_uris = {u for u in _fts_uris(tmp_path) if u.startswith("sessions/ws_a.md#")}
    assert turn_uris == {"sessions/ws_a.md#turn-1", "sessions/ws_a.md#turn-2"}


def test_tick_binds_gateway_telemetry_so_health_events_land(monkeypatch) -> None:
    """The scheduler runs run_tick on a bare thread; without binding a
    telemetry logger every `memory.health_check` / `staleness_detected`
    row is silently dropped (seen on home and box: zero rows). Bind the
    gateway session logger for the thread's lifetime, like the watcher."""
    import threading

    from durin.memory.health_check import HealthCheckScheduler
    from durin.telemetry.logger import current_telemetry

    seen: dict[str, object] = {}
    ran = threading.Event()

    class _Checker:
        _workspace = Path("/tmp")

        def run_tick(self):
            seen["logger"] = current_telemetry()
            ran.set()
            return {}

    sched = HealthCheckScheduler(_Checker(), interval_seconds=3600)
    sched.start()
    try:
        assert ran.wait(timeout=5.0)
    finally:
        sched.stop()
    tlog = seen.get("logger")
    assert tlog is not None and tlog.session_key == "gateway"
