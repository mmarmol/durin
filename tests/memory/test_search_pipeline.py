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
        if where == "class_name != 'reference'":
            keep = [r for r in keep if r["class_name"] != "reference"]
        return keep[:top_k]


def test_the_person_scope_reaches_the_vector_leg_as_a_prefilter(tmp_path):
    rows = [{"id": f"ref-{i}", "class_name": "reference", "path": f"r{i}.md"} for i in range(50)]
    rows.append({"id": "person:ada", "class_name": "entity_page", "path": "memory/entities/person/ada.md"})
    idx = _RecordingIndex(rows)
    result = run_search_pipeline(tmp_path, "ada", vector_index=idx, limit=3,
                                 scope=ScopePredicate.for_search("all"))
    assert idx.where == "class_name != 'reference'"
    assert [h.uri for h in result.hits] == ["person:ada"]


def test_the_lexical_leg_receives_the_type_set(tmp_path, monkeypatch):
    seen = {}
    import durin.memory.search_pipeline as sp

    def fake_lexical(index, decision, *, limit=50, emit=True, type_=None, include_types=None, exclude_types=None):
        seen["include"], seen["exclude"] = include_types, exclude_types
        return []

    monkeypatch.setattr(sp, "lexical_search", fake_lexical)
    run_search_pipeline(tmp_path, "ada", scope=ScopePredicate.for_search("library"))
    assert seen == {"include": ("reference",), "exclude": None}


def test_no_scope_means_no_filter_anywhere(tmp_path):
    idx = _RecordingIndex([{"id": "person:ada", "class_name": "entity_page", "path": "a.md"}])
    run_search_pipeline(tmp_path, "ada", vector_index=idx)
    assert idx.where is None
