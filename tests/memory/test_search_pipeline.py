"""End-to-end search pipeline orchestrator tests."""

from __future__ import annotations

from pathlib import Path

from durin.memory.entity_page import EntityPage
from durin.memory.indexer import rebuild_fts_index
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
