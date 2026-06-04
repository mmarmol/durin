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
