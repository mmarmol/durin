"""Workspace → FTS5 indexer (Phase 2 doc 02 §5 + §6).

The indexer walks `memory/` via the shared `walk_memory` helper and
writes one row per `.md` to the two FTS5 tables. It also keeps
`fts_meta.mtime` accurate so a subsequent rebuild can skip unchanged
files (incremental mode).
"""

from __future__ import annotations

from pathlib import Path

from durin.memory.entity_page import EntityPage
from durin.memory.fts_index import FTSIndex
from durin.memory.indexer import (
    IndexStats,
    rebuild_fts_index,
    reindex_one_file,
)
from durin.memory.schema import MemoryEntry
from durin.memory.storage import save_entry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(workspace: Path, type_: str, slug: str, body: str = "") -> Path:
    page = EntityPage(
        type=type_, name=slug.title(), aliases=[], body=body,
    )
    path = workspace / "memory" / "entities" / type_ / f"{slug}.md"
    page.save(path)
    return path


def _episodic(workspace: Path, name: str, headline: str = "h") -> Path:
    path = workspace / "memory" / "episodic" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = MemoryEntry(id=name, headline=headline, body="body")
    save_entry(entry, path)
    return path


# ---------------------------------------------------------------------------
# Bulk rebuild
# ---------------------------------------------------------------------------


def test_rebuild_indexes_entity_pages(tmp_path: Path) -> None:
    _entity(tmp_path, "person", "marcelo", body="Marcelo lives in Spain.")
    stats = rebuild_fts_index(tmp_path)
    assert isinstance(stats, IndexStats)
    assert stats.indexed >= 1
    with FTSIndex.open(tmp_path) as idx:
        hits = idx.search("Marcelo")
    assert any("person:marcelo" in h.uri for h in hits)


def test_rebuild_indexes_episodic_entries(tmp_path: Path) -> None:
    _episodic(tmp_path, "2026-05-23-foo", headline="found bug in auth")
    rebuild_fts_index(tmp_path)
    with FTSIndex.open(tmp_path) as idx:
        hits = idx.search("auth")
    assert any("episodic" in h.type for h in hits)


def test_rebuild_clears_old_rows(tmp_path: Path) -> None:
    """If a file is removed from disk between rebuilds, its row goes
    away on the next rebuild (clear → walk pattern)."""
    p = _entity(tmp_path, "person", "marcelo", body="hello")
    rebuild_fts_index(tmp_path)
    with FTSIndex.open(tmp_path) as idx:
        assert any(h.uri == "person:marcelo" for h in idx.search("hello"))
    p.unlink()
    rebuild_fts_index(tmp_path)
    with FTSIndex.open(tmp_path) as idx:
        assert not any(h.uri == "person:marcelo" for h in idx.search("hello"))


def test_rebuild_skips_archived_files(tmp_path: Path) -> None:
    """`walk_memory` excludes `memory/archive/**` by default — archived
    entries must not show up in the FTS index."""
    _entity(tmp_path, "person", "live", body="live entity")
    archive_dir = tmp_path / "memory" / "archive" / "episodic"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "old.md").write_text(
        "---\nid: old\nheadline: archived\n---\n\nold body\n",
        encoding="utf-8",
    )
    rebuild_fts_index(tmp_path)
    with FTSIndex.open(tmp_path) as idx:
        assert not any(h.uri == "old" for h in idx.search("archived"))


def test_rebuild_returns_index_stats(tmp_path: Path) -> None:
    _entity(tmp_path, "person", "a")
    _entity(tmp_path, "person", "b")
    _episodic(tmp_path, "e1")
    stats = rebuild_fts_index(tmp_path)
    assert stats.indexed >= 3
    assert stats.errors == 0


def test_rebuild_continues_on_one_malformed_file(tmp_path: Path) -> None:
    """A single corrupt .md doesn't abort the rebuild."""
    _entity(tmp_path, "person", "good")
    bad = tmp_path / "memory" / "episodic" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not a valid frontmatter\n", encoding="utf-8")
    stats = rebuild_fts_index(tmp_path)
    assert stats.indexed >= 1
    assert stats.errors >= 1


# ---------------------------------------------------------------------------
# Incremental — reindex_one_file (the re-index-on-write path)
# ---------------------------------------------------------------------------


def test_reindex_one_file_adds_row(tmp_path: Path) -> None:
    p = _entity(tmp_path, "person", "marcelo", body="Marcelo")
    reindex_one_file(tmp_path, p)
    with FTSIndex.open(tmp_path) as idx:
        assert any(h.uri == "person:marcelo" for h in idx.search("Marcelo"))


def test_reindex_one_file_overwrites_existing(tmp_path: Path) -> None:
    p = _entity(tmp_path, "person", "marcelo", body="first content")
    reindex_one_file(tmp_path, p)
    page = EntityPage.from_file(p)
    page.body = "second content"
    page.save(p)
    reindex_one_file(tmp_path, p)
    with FTSIndex.open(tmp_path) as idx:
        # The new content surfaces; the old does not.
        assert any(
            h.uri == "person:marcelo" for h in idx.search("second")
        )
        assert not any(
            h.uri == "person:marcelo" for h in idx.search("first")
        )


def test_reindex_one_file_handles_missing(tmp_path: Path) -> None:
    """If the file doesn't exist on disk (deleted between events),
    reindex_one_file removes the row."""
    p = _entity(tmp_path, "person", "marcelo")
    reindex_one_file(tmp_path, p)
    p.unlink()
    reindex_one_file(tmp_path, p)
    with FTSIndex.open(tmp_path) as idx:
        assert not any(
            h.uri == "person:marcelo" for h in idx.search("marcelo")
        )


def test_reindex_one_file_under_archive_is_noop(tmp_path: Path) -> None:
    """Files under archive/ are explicitly excluded — incremental
    re-index must respect the same rule as the bulk walker."""
    archive_dir = tmp_path / "memory" / "archive" / "episodic"
    archive_dir.mkdir(parents=True, exist_ok=True)
    p = archive_dir / "old.md"
    p.write_text(
        "---\nid: old\nheadline: ARCHIVED\n---\n\nbody\n",
        encoding="utf-8",
    )
    reindex_one_file(tmp_path, p)
    with FTSIndex.open(tmp_path) as idx:
        assert idx.count() == 0
