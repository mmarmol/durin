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


# ---------------------------------------------------------------------------
# references: the drift uri must match the indexed uri (`reference:<slug>`)
# ---------------------------------------------------------------------------


def _reference(workspace: Path, slug: str, body: str = "contenido") -> Path:
    path = workspace / "memory" / "references" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {slug.title()}\n\n{body}\n", encoding="utf-8")
    return path


def test_indexed_reference_is_not_flagged(tmp_path: Path) -> None:
    """A reference indexed under `reference:<slug>` must read back clean.

    `_uri_for` used to derive `memory/references/<slug>`, which never
    matched the `reference:<slug>` the indexer writes, so every reference
    was perpetually `missing_row` and re-indexed on every tick."""
    _reference(tmp_path, "forja")
    rebuild_fts_index(tmp_path)
    assert detect_index_staleness(tmp_path) == []


def test_unindexed_reference_is_missing_row_under_the_reference_uri(tmp_path: Path) -> None:
    _reference(tmp_path, "forja")
    issues = detect_index_staleness(tmp_path)
    assert {"uri": "reference:forja", "reason": "missing_row"} in issues


def test_reindex_deletes_reference_row_when_file_gone(tmp_path: Path) -> None:
    """The delete path derives the uri from the file; it must match the
    indexed `reference:<slug>`, or a removed reference leaves an orphan."""
    p = _reference(tmp_path, "forja")
    reindex_one_file(tmp_path, p, trigger="test")
    with FTSIndex.open(tmp_path) as idx:
        assert "reference:forja" in {u for u, _ in idx.known_uris()}
    p.unlink()
    reindex_one_file(tmp_path, p, trigger="test")
    with FTSIndex.open(tmp_path) as idx:
        assert "reference:forja" not in {u for u, _ in idx.known_uris()}


# ---------------------------------------------------------------------------
# sessions: drift detection covers rendered session files (turn-indexed)
# ---------------------------------------------------------------------------


def _session(workspace: Path, key: str, turns: int, extra: str = "") -> Path:
    sessions = workspace / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"## turn-{i}\n\nuser: mensaje {i}\n" for i in range(1, turns + 1))
    path = sessions / f"{key}.md"
    path.write_text(body + extra, encoding="utf-8")
    return path


def test_detects_an_unindexed_session_file(tmp_path: Path) -> None:
    _session(tmp_path, "ws_a", turns=2)
    issues = detect_index_staleness(tmp_path)
    assert {"uri": "sessions/ws_a.md", "reason": "missing_row"} in issues


def test_a_session_indexed_by_its_turns_reads_back_clean(tmp_path: Path) -> None:
    from durin.memory.indexer import reindex_session_file

    p = _session(tmp_path, "ws_a", turns=2)
    reindex_session_file(tmp_path, p)
    assert detect_index_staleness(tmp_path) == []


def test_a_live_session_turn_row_is_not_pruned_as_an_orphan(tmp_path: Path) -> None:
    """The orphan pass keys on turn uris (`sessions/<k>.md#turn-N`); a
    turn row is an orphan only when its session FILE is gone, never
    because the per-file uri is absent from the memory walk."""
    from durin.memory.indexer import reindex_session_file

    p = _session(tmp_path, "ws_a", turns=2)
    reindex_session_file(tmp_path, p)
    issues = detect_index_staleness(tmp_path)
    assert not any(i["reason"] == "row_for_missing_file" for i in issues)


def test_a_deleted_session_turn_row_is_flagged_orphan(tmp_path: Path) -> None:
    from durin.memory.indexer import reindex_session_file

    p = _session(tmp_path, "ws_a", turns=2)
    reindex_session_file(tmp_path, p)
    p.unlink()
    issues = detect_index_staleness(tmp_path)
    orphans = [i for i in issues if i["reason"] == "row_for_missing_file"]
    assert orphans and all(u["uri"].startswith("sessions/ws_a.md#") for u in orphans)


def test_a_touched_session_with_no_new_turns_is_not_flagged(tmp_path: Path) -> None:
    """A consolidation annotation rewrites old turns and bumps mtime
    without adding a turn. The indexed text of those turns is unchanged
    (the annotation is never searched), so the session must NOT be
    flagged stale — mtime alone must not re-flag it every tick."""
    from durin.memory.indexer import reindex_session_file

    p = _session(tmp_path, "ws_a", turns=2)
    reindex_session_file(tmp_path, p)
    later = time.time() + 60
    os.utime(p, (later, later))  # touched, same turns
    assert detect_index_staleness(tmp_path) == []


def test_a_session_with_a_new_turn_is_mtime_lag(tmp_path: Path) -> None:
    from durin.memory.indexer import reindex_session_file

    p = _session(tmp_path, "ws_a", turns=2)
    reindex_session_file(tmp_path, p)
    _session(tmp_path, "ws_a", turns=3)  # append a real turn, bumps mtime
    issues = detect_index_staleness(tmp_path)
    assert {"uri": "sessions/ws_a.md", "reason": "mtime_lag"} in [
        {"uri": i["uri"], "reason": i["reason"]} for i in issues
    ]


def test_a_session_with_no_turns_is_not_flagged(tmp_path: Path) -> None:
    """A header-only session (created, never messaged) has nothing to
    index; flagging it `missing_row` would re-run a no-op repair every
    tick forever."""
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "api_x.md").write_text(
        "# Session api:x\n\n- Created: 2026-08-21\n", encoding="utf-8",
    )
    assert detect_index_staleness(tmp_path) == []
