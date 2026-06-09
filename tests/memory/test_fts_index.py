"""FTS5 dual-table lexical index for memory.

Per `docs/architecture/memory/02_indexing.md` §5: one SQLite database at
`.durin/index/fts.sqlite` with two FTS5 virtual tables sharing a
`fts_meta` bookkeeping table. The two virtual tables:

  - `memory_fts` — `tokenize='unicode61 remove_diacritics 2'` for
    Latin / Cyrillic / Greek / Arabic.
  - `memory_fts_trigram` — `tokenize='trigram'` for CJK and substring
    queries.

Every memory write goes to both tables. Routing at *search* time is
the search pipeline's job (Phase 3); the indexer's job is to keep the
tables in sync.
"""

from __future__ import annotations

from pathlib import Path

from durin.memory.fts_index import FTSIndex, fts_index_path

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_index_path_under_durin_index(tmp_path: Path) -> None:
    assert fts_index_path(tmp_path) == tmp_path / ".durin" / "index" / "fts.sqlite"


def test_open_creates_parent_dirs(tmp_path: Path) -> None:
    """`.durin/index/` is created lazily — caller doesn't pre-mkdir."""
    assert not (tmp_path / ".durin").exists()
    with FTSIndex.open(tmp_path) as idx:
        assert idx is not None
    assert fts_index_path(tmp_path).exists()


def test_open_initialises_schema(tmp_path: Path) -> None:
    """Both FTS5 virtual tables + the meta table exist after open."""
    import sqlite3
    with FTSIndex.open(tmp_path):
        pass
    conn = sqlite3.connect(fts_index_path(tmp_path))
    try:
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "OR type='view'"
            )
        }
    finally:
        conn.close()
    # FTS5 creates auxiliary tables; we only care that ours exist.
    assert "memory_fts" in names
    assert "memory_fts_trigram" in names
    assert "fts_meta" in names


# ---------------------------------------------------------------------------
# Upsert + search round-trip
# ---------------------------------------------------------------------------


def test_upsert_then_search_latin(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="person:marcelo",
            path="memory/entities/person/marcelo.md",
            type_="entity",
            entity_type="person",
            text="Marcelo Marmol lives in Spain",
            mtime=1700000000.0,
        )
        hits = idx.search("Marcelo")
    assert any(h.uri == "person:marcelo" for h in hits)


def test_search_diacritics_normalized(tmp_path: Path) -> None:
    """`remove_diacritics 2` means a query for plain text matches the
    accented version on disk."""
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="person:marmol",
            path="x.md",
            type_="entity",
            entity_type="person",
            text="Marcelo Mármol",
            mtime=1.0,
        )
        # Plain `marmol` matches accented document.
        hits = idx.search("Marmol")
    assert any(h.uri == "person:marmol" for h in hits)


def test_search_cjk_via_trigram(tmp_path: Path) -> None:
    """CJK queries go via the trigram table (the search pipeline routes;
    here we just verify the table accepts and returns CJK content)."""
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="person:masailuo",
            path="x.md",
            type_="entity",
            entity_type="person",
            text="马塞洛 is the Chinese transliteration",
            mtime=1.0,
        )
        hits = idx.search_trigram("马塞洛")
    assert any(h.uri == "person:masailuo" for h in hits)


def test_search_substring_via_trigram(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="topic:autocompaction",
            path="x.md",
            type_="topic",
            entity_type=None,
            text="autocompaction is the loop guard",
            mtime=1.0,
        )
        # 4-char substring; trigram should match.
        hits = idx.search_trigram("comp")
    assert any(h.uri == "topic:autocompaction" for h in hits)


# ---------------------------------------------------------------------------
# Upsert is idempotent (replaces existing row, no duplicates)
# ---------------------------------------------------------------------------


def test_upsert_replaces_existing(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="person:x",
            path="x.md",
            type_="entity",
            entity_type="person",
            text="first version",
            mtime=1.0,
        )
        idx.upsert(
            uri="person:x",
            path="x.md",
            type_="entity",
            entity_type="person",
            text="second version",
            mtime=2.0,
        )
        # The old content is no longer searchable.
        hits = idx.search("first")
        assert not any(h.uri == "person:x" for h in hits)
        # The new content is.
        hits2 = idx.search("second")
        assert any(h.uri == "person:x" for h in hits2)


def test_upsert_mtime_recorded_in_meta(tmp_path: Path) -> None:
    """`fts_meta.mtime` lets the indexer skip files that haven't
    changed since the last sync."""
    import sqlite3
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="person:x", path="x.md", type_="entity",
            entity_type="person", text="t", mtime=42.0,
        )
    conn = sqlite3.connect(fts_index_path(tmp_path))
    try:
        row = conn.execute(
            "SELECT mtime FROM fts_meta WHERE uri = 'person:x'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 42.0


# ---------------------------------------------------------------------------
# delete_by_uri removes from both tables
# ---------------------------------------------------------------------------


def test_delete_removes_from_both_tables(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="person:gone", path="g.md", type_="entity",
            entity_type="person", text="the entity to delete",
            mtime=1.0,
        )
        # Confirm it's present.
        assert any(
            h.uri == "person:gone" for h in idx.search("delete")
        )
        idx.delete_by_uri("person:gone")
        assert not any(
            h.uri == "person:gone" for h in idx.search("delete")
        )
        assert not any(
            h.uri == "person:gone" for h in idx.search_trigram("delete")
        )


def test_delete_missing_uri_is_noop(tmp_path: Path) -> None:
    """Deleting a uri that never existed is fine — useful for the
    file-watcher coalescing path."""
    with FTSIndex.open(tmp_path) as idx:
        idx.delete_by_uri("person:never_existed")  # must not raise


# ---------------------------------------------------------------------------
# count / clear (operational helpers for `durin reindex`)
# ---------------------------------------------------------------------------


def test_count_returns_row_count(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        assert idx.count() == 0
        idx.upsert(uri="a", path="a.md", type_="entity",
                   entity_type=None, text="t", mtime=1.0)
        idx.upsert(uri="b", path="b.md", type_="entity",
                   entity_type=None, text="t", mtime=1.0)
        assert idx.count() == 2


def test_clear_empties_both_tables(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(uri="a", path="a.md", type_="entity",
                   entity_type=None, text="t", mtime=1.0)
        idx.clear()
        assert idx.count() == 0
        assert idx.search("t") == []
        assert idx.search_trigram("t") == []


# ---------------------------------------------------------------------------
# Result shape — search results carry uri + type + path + a snippet
# ---------------------------------------------------------------------------


def test_search_result_fields(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="person:marcelo",
            path="memory/entities/person/marcelo.md",
            type_="entity",
            entity_type="person",
            text="Marcelo Marmol architect",
            mtime=1.0,
        )
        hit = idx.search("Marcelo")[0]
    assert hit.uri == "person:marcelo"
    assert hit.path == "memory/entities/person/marcelo.md"
    assert hit.type == "entity"
    assert hit.entity_type == "person"


# ---------------------------------------------------------------------------
# Context manager + manual close both work
# ---------------------------------------------------------------------------


def test_explicit_close_after_open(tmp_path: Path) -> None:
    idx = FTSIndex.open(tmp_path)
    idx.upsert(uri="x", path="x.md", type_="entity",
               entity_type=None, text="t", mtime=1.0)
    idx.close()
    # Re-open and confirm persistence.
    with FTSIndex.open(tmp_path) as idx2:
        assert any(h.uri == "x" for h in idx2.search("t"))


# ---------------------------------------------------------------------------
# Ranking — MATCH results must come back in BM25 order, not rowid order
# ---------------------------------------------------------------------------


def test_search_returns_bm25_order_not_insertion_order(tmp_path: Path) -> None:
    """FTS5 returns rowid (insertion) order unless the query says
    ORDER BY rank. The pipeline feeds these hits to RRF as a ranked
    list, so insertion order silently breaks the lexical ranking —
    rows indexed later (e.g. sessions, indexed after memory/) would
    always lose regardless of relevance."""
    with FTSIndex.open(tmp_path) as idx:
        # Inserted FIRST, weak match: one needle among many tokens.
        idx.upsert(
            uri="weak", path="a.md", type_="episodic", entity_type=None,
            text="needle alpha beta gamma delta epsilon zeta eta theta",
            mtime=1.0,
        )
        # Inserted SECOND, strong match: the needle dominates.
        idx.upsert(
            uri="strong", path="b.md", type_="episodic", entity_type=None,
            text="needle needle needle needle",
            mtime=1.0,
        )
        uris = [h.uri for h in idx.search("needle")]
    assert uris == ["strong", "weak"]


def test_search_trigram_returns_bm25_order(tmp_path: Path) -> None:
    """Same ranking contract for the trigram (CJK) table. Tokens must
    be >=3 chars — the trigram tokenizer can't index shorter ones
    (that's why the query router sends short CJK to LIKE)."""
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="weak", path="a.md", type_="episodic", entity_type=None,
            text="記憶装置 検索結果 設計文書 実装計画 評価基準",
            mtime=1.0,
        )
        idx.upsert(
            uri="strong", path="b.md", type_="episodic", entity_type=None,
            text="記憶装置 記憶装置 記憶装置 記憶装置",
            mtime=1.0,
        )
        uris = [h.uri for h in idx.search_trigram("記憶装置")]
    assert uris == ["strong", "weak"]
