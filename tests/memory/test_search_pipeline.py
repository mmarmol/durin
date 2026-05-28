"""End-to-end orchestrator tests (doc 03 §1)."""

from __future__ import annotations

from pathlib import Path

import pytest

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
