"""End-to-end search pipeline orchestrator tests."""

from __future__ import annotations

from pathlib import Path

from durin.memory.entity_page import EntityPage
from durin.memory.indexer import rebuild_fts_index
from durin.memory.scope import ScopePredicate
from durin.memory.search_pipeline import (
    SearchPipelineResult,
    run_search_pipeline,
)


def _seed(workspace: Path) -> None:
    page = EntityPage(
        type="person", name="Marcelo", aliases=["Marcelo Marmol"],
        body="Architect of durin. Lives in Spain.",
    )
    page.save(workspace / "memory" / "entities" / "person" / "marcelo.md")
    rebuild_fts_index(workspace)


def test_lexical_only_pipeline_returns_hits(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = run_search_pipeline(tmp_path, "Marcelo")
    assert isinstance(result, SearchPipelineResult)
    assert any(h.uri == "person:marcelo" for h in result.hits)
    assert result.lexical_count >= 1
    assert result.vector_count == 0  # no vector_index supplied


def test_keywords_param_propagates_through(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = run_search_pipeline(
        tmp_path, "Marcelo", keywords="durin",
    )
    # Result must still be valid; the boost happens inside RRF.
    assert isinstance(result, SearchPipelineResult)


def test_empty_workspace_returns_empty(tmp_path: Path) -> None:
    result = run_search_pipeline(tmp_path, "anything")
    assert result.hits == []
    assert result.lexical_count == 0


def test_leading_boolean_keyword_query_does_not_degrade(
    tmp_path: Path,
) -> None:
    """A natural-language query beginning with "not" must not trip the
    FTS5 boolean parser and silently drop the lexical tier (and the
    grep-verify boost, which quotes the same target). Pre-fix this
    raised `fts5: syntax error near "NOT"` inside both safe wrappers.
    """
    page = EntityPage(
        type="topic", name="deploy-notes",
        body="not sure which gateway to deploy next",
    )
    page.save(tmp_path / "memory" / "entities" / "topic" / "deploy.md")
    rebuild_fts_index(tmp_path)
    result = run_search_pipeline(
        tmp_path, "not sure which gateway", keywords="not sure",
    )
    assert "lexical" not in result.recovered_from
    assert result.lexical_count >= 1
    assert any("deploy" in h.uri for h in result.hits)


def test_limit_caps_results(tmp_path: Path) -> None:
    # Seed many episodic entries.
    from durin.memory.schema import MemoryEntry
    from durin.memory.storage import save_entry
    epi_dir = tmp_path / "memory" / "episodic"
    epi_dir.mkdir(parents=True, exist_ok=True)
    for i in range(20):
        save_entry(
            MemoryEntry(id=f"e{i}", headline=f"common_keyword entry {i}",
                        body="body"),
            epi_dir / f"e{i}.md",
        )
    rebuild_fts_index(tmp_path)
    result = run_search_pipeline(tmp_path, "common_keyword", limit=3)
    assert len(result.hits) <= 3


def test_vector_failure_degrades_to_lexical_only(tmp_path: Path) -> None:
    """If the vector_index raises, the pipeline doesn't crash — it
    just runs lexical-only."""
    _seed(tmp_path)

    class _BrokenIndex:
        def search(self, *_a, **_kw):
            raise RuntimeError("lance is dead")

    result = run_search_pipeline(
        tmp_path, "Marcelo", vector_index=_BrokenIndex(),
    )
    assert result.vector_count == 0
    assert any(h.uri == "person:marcelo" for h in result.hits)


def test_fake_vector_index_integrated(tmp_path: Path) -> None:
    """When a duck-typed vector index returns rows, they enter the
    fusion — sources counted, ranks recorded."""
    _seed(tmp_path)

    class _FakeIndex:
        def search(self, query, top_k=50):
            return [
                {"uri": "person:marcelo", "type": "entity_page",
                 "path": "memory/entities/person/marcelo.md"},
            ]

    result = run_search_pipeline(
        tmp_path, "Marcelo", vector_index=_FakeIndex(),
    )
    assert result.vector_count == 1
    # Hit must be tagged as entity (entity_page → entity normalised).
    hit = next((h for h in result.hits if h.uri == "person:marcelo"), None)
    assert hit is not None
    assert hit.type == "entity"


def test_hit_carries_the_indexed_entity_tags(tmp_path: Path) -> None:
    """The vector row's `entities` column must reach the `SectionedHit`.

    The reranker already read it off the metadata dict, but the hit built for
    the renderer dropped it — so a tagged entry's block never showed the
    `Entities:` tail that points the agent at the canonical pages."""
    _seed(tmp_path)

    class _TaggedIndex:
        def search(self, query, top_k=50):
            return [
                {
                    "id": "abc123def456",
                    "class_name": "session_summary",
                    "summary": "A conversation about Marcelo",
                    "headline": "Marcelo mentioned this",
                    "valid_from": "2026-01-15T10:00:00",
                    "entities": ["person:marcelo", "project:durin"],
                    "path": "memory/session_summary/abc123def456.md",
                    "_distance": 14.2,
                },
            ]

    result = run_search_pipeline(
        tmp_path, "Marcelo", vector_index=_TaggedIndex(),
    )
    hit = next(
        (h for h in result.hits if h.uri == "memory/session_summary/abc123def456"),
        None,
    )
    assert hit is not None
    assert hit.entities == ("person:marcelo", "project:durin")


def test_vector_index_native_row_shape_is_accepted(tmp_path: Path) -> None:
    """Audit H1 (2026-05-29) + H28 (2026-05-30): the real
    ``VectorIndex.search()`` emits rows with ``id`` / ``class_name`` /
    ``path`` — NOT ``uri`` / ``type``. Pre-H1 the pipeline filtered
    every row out (``if "uri" in h``), so warm-tier vector retrieval
    was silently lexical-only since the Phase 3 orchestrator landed.
    H1 fixed the filtering. H28 fixed the URI format mismatch: the
    normaliser now builds ``memory/<class>/<id>`` URIs to match what
    the FTS indexer writes (``indexer._payload_for``); pre-H28 vector
    used bare ``<id>`` and RRF couldn't fuse vector + FTS hits for
    the same entry.
    """
    _seed(tmp_path)

    class _NativeShapeIndex:
        """Mirror the production VectorIndex row shape exactly."""

        def search(self, query, top_k=50):
            return [
                {
                    "id": "person:marcelo",        # NOT 'uri'
                    "class_name": "entity_page",   # NOT 'type'
                    "summary": "Marcelo (Marcelo Marmol)",
                    "headline": "Marcelo",
                    "valid_from": "",
                    "entities": [],
                    "path": "memory/entities/person/marcelo.md",
                    "_distance": 13.5,
                },
                {
                    "id": "abc123def456",
                    "class_name": "episodic",
                    "summary": "An episodic entry mentioning Marcelo",
                    "headline": "Marcelo mentioned this",
                    "valid_from": "2026-01-15T10:00:00",
                    "entities": ["person:marcelo"],
                    "path": "memory/episodic/abc123def456.md",
                    "_distance": 14.2,
                },
            ]

    result = run_search_pipeline(
        tmp_path, "Marcelo", vector_index=_NativeShapeIndex(),
    )
    # Both rows must reach the fusion stage — vector_count counts the
    # rows the pipeline successfully accepted from the vector source.
    assert result.vector_count == 2, (
        f"vector_count={result.vector_count}; expected 2. The pipeline "
        "is silently filtering native-shape rows."
    )
    # Entity URI (entity_ref) must surface in the fused hits.
    assert any(h.uri == "person:marcelo" for h in result.hits)
    # Episodic URI must use FTS-compatible `memory/<class>/<id>` shape
    # so RRF can fuse vector + FTS hits for the same entry (H28).
    assert any(h.uri == "memory/episodic/abc123def456" for h in result.hits)
    # Entity-page hit type must normalise to 'entity' downstream.
    entity_hit = next(
        (h for h in result.hits if h.uri == "person:marcelo"), None,
    )
    assert entity_hit is not None
    assert entity_hit.type == "entity"


# ---------------------------------------------------------------------------
# scope predicate — carried into both index legs
# ---------------------------------------------------------------------------


class _RecordingIndex:
    def __init__(self, rows):
        self.rows = rows
        self.where = "unset"

    def search(self, query, *, top_k=10, where=None):
        self.where = where
        keep = self.rows
        if where == "class_name NOT IN ('reference', 'corpus')":
            keep = [r for r in keep if r["class_name"] not in ("reference", "corpus")]
        return keep[:top_k]


def test_the_person_scope_reaches_the_vector_leg_as_a_prefilter(tmp_path):
    rows = [{"id": f"ref-{i}", "class_name": "reference", "path": f"r{i}.md"} for i in range(50)]
    rows.append({"id": "person:ada", "class_name": "entity_page", "path": "memory/entities/person/ada.md"})
    idx = _RecordingIndex(rows)
    result = run_search_pipeline(tmp_path, "ada", vector_index=idx, limit=3,
                                 scope=ScopePredicate.for_search("all"))
    assert idx.where == "class_name NOT IN ('reference', 'corpus')"
    assert [h.uri for h in result.hits] == ["person:ada"]


def test_the_lexical_leg_receives_the_type_set(tmp_path, monkeypatch):
    seen = {}
    import durin.memory.search_pipeline as sp

    def fake_lexical(index, decision, *, limit=50, emit=True, type_=None, include_types=None, exclude_types=None):
        seen["include"], seen["exclude"] = include_types, exclude_types
        return []

    monkeypatch.setattr(sp, "lexical_search", fake_lexical)
    run_search_pipeline(tmp_path, "ada", scope=ScopePredicate.for_search("library"))
    assert seen == {"include": ("reference", "corpus"), "exclude": None}


def test_no_scope_means_no_filter_anywhere(tmp_path):
    idx = _RecordingIndex([{"id": "person:ada", "class_name": "entity_page", "path": "a.md"}])
    run_search_pipeline(tmp_path, "ada", vector_index=idx)
    assert idx.where is None


def _grep_hits_of_every_kind(workspace, query, *, recovery, **kwargs):
    return [
        {"uri": "sessions/websocket_x.md#turn-3", "type": "session",
         "path": "sessions/websocket_x.md#turn-3", "snippet": "…"},
        {"uri": "memory/session_summary/s1", "type": "session_summary",
         "path": "memory/session_summary/s1", "snippet": "…"},
        {"uri": "memory/episodic/e1", "type": "episodic",
         "path": "memory/episodic/e1", "snippet": "…"},
        {"uri": "person:ada", "type": "entity",
         "path": "memory/entity_page/person:ada", "snippet": "Ada"},
    ]


def test_undreamed_scope_keeps_only_session_material_on_the_grep_leg(tmp_path, monkeypatch):
    """The grep leg has no index to filter, so `scope=undreamed` keeps the
    raw session turns and the session summaries and drops the distilled
    entries and entity pages the walk also turned up."""
    import durin.memory.search_pipeline as sp

    monkeypatch.setattr(sp, "_safe_grep_fallback", _grep_hits_of_every_kind)
    result = run_search_pipeline(
        tmp_path, "ada", scope=ScopePredicate.for_search("undreamed"),
    )
    assert sorted(h.uri for h in result.hits) == [
        "memory/session_summary/s1", "sessions/websocket_x.md#turn-3",
    ]


def test_dreamed_scope_drops_session_material_from_the_grep_leg(tmp_path, monkeypatch):
    import durin.memory.search_pipeline as sp

    monkeypatch.setattr(sp, "_safe_grep_fallback", _grep_hits_of_every_kind)
    result = run_search_pipeline(
        tmp_path, "ada", scope=ScopePredicate.for_search("dreamed"),
    )
    assert sorted(h.uri for h in result.hits) == ["memory/episodic/e1", "person:ada"]


def test_entity_pages_scope_filters_the_grep_leg_to_entity_refs(tmp_path, monkeypatch):
    """The grep leg has no index to filter, so under `entity_pages()`
    scope it must keep only entity-ref-shaped uris (`<type>:<slug>`) and
    drop anything else the walk turned up, such as a session hit."""
    import durin.memory.search_pipeline as sp

    def fake_grep(workspace, query, *, recovery, **kwargs):
        return [
            {"uri": "sessions/websocket_x.md#turn-3", "type": "session",
             "path": "sessions/websocket_x.md#turn-3", "snippet": "…"},
            {"uri": "person:ada", "type": "entity",
             "path": "memory/entity_page/person:ada", "snippet": "Ada"},
        ]

    monkeypatch.setattr(sp, "_safe_grep_fallback", fake_grep)
    result = run_search_pipeline(tmp_path, "ada", scope=ScopePredicate.entity_pages())
    assert [h.uri for h in result.hits] == ["person:ada"]


def test_grep_leg_reads_only_files_the_index_does_not_hold(tmp_path):
    """The grep leg is the recovery path for what the indexes have not
    caught up with; a file the FTS index holds unchanged is not read
    again (the lexical leg already finds it), and the pipeline reports
    what the walk actually read."""
    from durin.memory.indexer import reindex_one_file
    from durin.memory.store import store_memory

    indexed = store_memory(tmp_path, content="la forja vieja de Mithral Hall", class_name="episodic")
    reindex_one_file(tmp_path, Path(indexed["path"]))

    result = run_search_pipeline(tmp_path, "forja vieja")
    assert (result.grep_scanned, result.grep_skipped) == (0, 1)
    assert [h.uri for h in result.hits] == [f"memory/episodic/{indexed['id']}"]

    fresh = store_memory(tmp_path, content="el yunque nuevo del herrero", class_name="episodic")
    result = run_search_pipeline(tmp_path, "yunque nuevo")
    assert (result.grep_scanned, result.grep_skipped) == (1, 1)
    assert [h.uri for h in result.hits] == [f"memory/episodic/{fresh['id']}"]


def test_a_lexical_only_hit_carries_the_entry_display_fields(tmp_path):
    """An FTS row holds only uri, path and type. A hit the lexical leg
    alone surfaced (no vector index here) must still carry the entry's
    headline, summary and body length, or its block renders empty — the
    grep leg used to paper over this by re-reading every indexed file."""
    from durin.memory.indexer import reindex_one_file
    from durin.memory.store import store_memory

    r = store_memory(tmp_path, content="marcelo prefers pytest over unittest", class_name="episodic")
    reindex_one_file(tmp_path, Path(r["path"]))

    result = run_search_pipeline(tmp_path, "pytest")

    assert (result.grep_scanned, result.grep_skipped) == (0, 1)
    [hit] = result.hits
    assert hit.uri == f"memory/episodic/{r['id']}"
    assert hit.snippet == "marcelo prefers pytest over unittest"  # the headline, no grep snippet
    assert "pytest" in hit.summary
    assert hit.body_length > 0
