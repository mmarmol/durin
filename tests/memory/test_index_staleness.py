"""Drift detector between markdown source and FTS5 index.

The health-check cron compares files under `memory/` to rows in `fts_meta`
and emits `memory.index.staleness_detected` events for each discrepancy.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from durin.memory.entity_page import EntityPage
from durin.memory.fts_index import FTSIndex
from durin.memory.indexer import (
    detect_index_staleness,
    rebuild_fts_index,
    reindex_one_file,
)


def _entity(workspace: Path, slug: str) -> Path:
    page = EntityPage(type="person", name=slug.title(), aliases=[])
    path = workspace / "memory" / "entities" / "person" / f"{slug}.md"
    page.save(path)
    return path


def test_no_drift_returns_empty_list(tmp_path: Path) -> None:
    _entity(tmp_path, "marcelo")
    rebuild_fts_index(tmp_path)
    assert detect_index_staleness(tmp_path) == []


def test_detects_missing_row(tmp_path: Path) -> None:
    """File exists on disk; no row in the index → missing_row."""
    _entity(tmp_path, "marcelo")
    issues = detect_index_staleness(tmp_path)
    assert {"uri": "person:marcelo", "reason": "missing_row"} in issues


def test_detects_row_for_missing_file(tmp_path: Path) -> None:
    """Row exists in the index; file deleted from disk →
    row_for_missing_file."""
    p = _entity(tmp_path, "ghost")
    rebuild_fts_index(tmp_path)
    p.unlink()
    issues = detect_index_staleness(tmp_path)
    assert {"uri": "person:ghost", "reason": "row_for_missing_file"} in issues


def _stable_entry(workspace: Path, content: str = "x") -> tuple[Path, str]:
    """Write + return (path, uri) for a stable memory entry."""
    import datetime

    from durin.memory.provenance import author_scope
    from durin.memory.store import store_memory

    with author_scope("agent_created"):
        res = store_memory(
            workspace, content=content, class_name="stable",
            entities=["company:mxhero"],
            valid_from=datetime.date(2026, 6, 4),
        )
    path = workspace / "memory" / "stable" / f"{res['id']}.md"
    return path, f"memory/stable/{res['id']}"


def test_no_drift_for_present_entry(tmp_path: Path) -> None:
    """Regression: a present, correctly-indexed ENTRY must not be flagged.

    `_uri_for` returned the bare stem for entries while the index stores
    `memory/<class>/<id>`, so every present entry was double-flagged
    (row_for_missing_file + missing_row) and needlessly re-indexed on
    every health-check tick.
    """
    path, _uri = _stable_entry(tmp_path)
    reindex_one_file(tmp_path, path, trigger="test")
    assert detect_index_staleness(tmp_path) == []


def test_reindex_deletes_entry_row_when_file_gone(tmp_path: Path) -> None:
    """reindex_one_file on a removed entry must drop its FTS row — the
    derived uri must match the indexed `memory/<class>/<id>` form, else
    forget / drift-repair silently leave an orphan row."""
    path, uri = _stable_entry(tmp_path)
    reindex_one_file(tmp_path, path, trigger="test")
    with FTSIndex.open(tmp_path) as idx:
        assert uri in {u for u, _ in idx.known_uris()}
    path.unlink()
    reindex_one_file(tmp_path, path, trigger="test")
    with FTSIndex.open(tmp_path) as idx:
        assert uri not in {u for u, _ in idx.known_uris()}


def test_detects_mtime_lag(tmp_path: Path) -> None:
    """File modified after the indexer wrote its row → mtime_lag."""
    p = _entity(tmp_path, "marcelo")
    rebuild_fts_index(tmp_path)
    # Force a future mtime so even a fast-running test catches the lag.
    later = time.time() + 60
    os.utime(p, (later, later))
    issues = detect_index_staleness(tmp_path)
    assert any(
        i["uri"] == "person:marcelo" and i["reason"] == "mtime_lag"
        for i in issues
    )


def test_clean_after_reindex(tmp_path: Path) -> None:
    """After we re-touch + reindex, the drift goes away."""
    p = _entity(tmp_path, "marcelo")
    rebuild_fts_index(tmp_path)
    later = time.time() + 60
    os.utime(p, (later, later))
    assert detect_index_staleness(tmp_path)  # has drift
    reindex_one_file(tmp_path, p)
    # mtime_lag specifically — `missing_row` shouldn't fire either.
    assert detect_index_staleness(tmp_path) == []
