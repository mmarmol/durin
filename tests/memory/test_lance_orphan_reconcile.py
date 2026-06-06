"""Lance↔disk reconcile: prune vector-index rows whose backing file is gone.

These cover the failure mode the FTS-driven staleness check is blind to —
rows that live ONLY in the Lance table and never in ``fts_meta`` (orphans
from an out-of-band ``rm -rf memory/<dir>``, a reinstall, or a partial
reindex that rebuilt FTS but not Lance). The real-world trigger was 203
``helpjuice`` corpus chunks stranded in Lance after a cleanup, surfacing in
the webui "Entradas" tab and 404ing on click.

lancedb is an optional extra; every test self-skips when it's absent so CI
(which installs durin without ``[memory]``) stays green.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _make_lance_table(ws: Path, rows: list[tuple[str, str]]) -> None:
    """Create the ``memory_entries`` table directly with (id, path) rows.

    Model-free: dummy 2-d vectors, faithful column schema. Lets a test
    construct an arbitrary index state (including orphans) without the
    embedding model.
    """
    lancedb = pytest.importorskip("lancedb")
    from durin.memory.vector_index import _INDEX_PATH, _TABLE_NAME

    uri = str(ws.joinpath(*_INDEX_PATH))
    Path(uri).mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(uri)
    records = [
        {
            "id": rid,
            "class_name": "corpus",
            "summary": "",
            "headline": "",
            "body_length": 0,
            "valid_from": "",
            "entities": [],
            "path": rpath,
            "vector": [0.0, 0.0],
        }
        for rid, rpath in rows
    ]
    if _TABLE_NAME in db.list_tables().tables:
        db.drop_table(_TABLE_NAME)
    db.create_table(_TABLE_NAME, data=records)


def _lance_ids(ws: Path) -> set[str]:
    import lancedb

    from durin.memory.vector_index import _INDEX_PATH, _TABLE_NAME

    db = lancedb.connect(str(ws.joinpath(*_INDEX_PATH)))
    t = db.open_table(_TABLE_NAME)
    total = t.count_rows()
    if total == 0:
        return set()
    return {r["id"] for r in t.search().select(["id"]).limit(total).to_list()}


def _touch(ws: Path, rel: str) -> None:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n---\nbody\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# prune_orphan_rows — the reconcile primitive
# ---------------------------------------------------------------------------


def test_prune_drops_missing_file_keeps_present(tmp_path: Path) -> None:
    pytest.importorskip("lancedb")
    from durin.memory.vector_index import prune_orphan_rows

    _touch(tmp_path, "memory/stable/keep.md")
    _make_lance_table(
        tmp_path,
        [
            ("keep", "memory/stable/keep.md"),  # backed by a real file
            ("ghost", "memory/corpus/ghost.md"),  # no file on disk
        ],
    )
    pruned = prune_orphan_rows(tmp_path)
    assert pruned == ["ghost"]
    assert _lance_ids(tmp_path) == {"keep"}


def test_prune_noop_when_all_present(tmp_path: Path) -> None:
    pytest.importorskip("lancedb")
    from durin.memory.vector_index import prune_orphan_rows

    _touch(tmp_path, "memory/stable/keep.md")
    _make_lance_table(tmp_path, [("keep", "memory/stable/keep.md")])
    assert prune_orphan_rows(tmp_path) == []
    assert _lance_ids(tmp_path) == {"keep"}


def test_prune_skips_empty_path(tmp_path: Path) -> None:
    # Conservative: a row with no path can't be verified, so it is left
    # alone rather than deleted.
    pytest.importorskip("lancedb")
    from durin.memory.vector_index import prune_orphan_rows

    _make_lance_table(tmp_path, [("nopath", "")])
    assert prune_orphan_rows(tmp_path) == []
    assert _lance_ids(tmp_path) == {"nopath"}


def test_prune_noop_when_no_table(tmp_path: Path) -> None:
    # No lance dir / table at all — clean no-op, never raises.
    from durin.memory.vector_index import prune_orphan_rows

    assert prune_orphan_rows(tmp_path) == []


# ---------------------------------------------------------------------------
# health-check tick reconciles lance-only orphans (the helpjuice scenario)
# ---------------------------------------------------------------------------


def test_tick_reconciles_lance_only_orphan(tmp_path: Path) -> None:
    pytest.importorskip("lancedb")
    from durin.memory.health_check import HealthChecker

    # Rows exist ONLY in Lance — there is no fts.sqlite, so the FTS-driven
    # staleness pass can never see them. The reconcile pass must.
    _make_lance_table(
        tmp_path,
        [
            ("ghost1", "memory/corpus/ghost1.md"),
            ("ghost2", "memory/corpus/ghost2.md"),
        ],
    )
    payload = HealthChecker(tmp_path).run_tick()
    assert _lance_ids(tmp_path) == set()
    assert payload.get("lance_orphans_pruned") == 2


# ---------------------------------------------------------------------------
# gap 1 — watcher delete is symmetric (FTS + Lance) when a file vanishes
# ---------------------------------------------------------------------------


def test_reindex_one_file_deletes_lance_row_on_vanish(tmp_path: Path) -> None:
    pytest.importorskip("lancedb")
    from durin.memory.indexer import reindex_one_file

    entry = tmp_path / "memory" / "stable" / "abc123.md"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("---\n---\nbody\n", encoding="utf-8")
    _make_lance_table(tmp_path, [("abc123", "memory/stable/abc123.md")])
    entry.unlink()  # file vanishes out from under the watcher
    reindex_one_file(tmp_path, entry, trigger="test")
    assert "abc123" not in _lance_ids(tmp_path)


def test_vector_id_for_uri_mapping() -> None:
    from durin.memory.vector_index import vector_id_for_uri

    assert vector_id_for_uri("memory/stable/abc123") == "abc123"
    assert vector_id_for_uri("person:marcelo") == "person:marcelo"
    assert vector_id_for_uri("skill/web-scraping") == "skill/web-scraping"
