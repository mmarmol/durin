"""Tests for the LanceDB-backed vector index.

These use real lancedb in ``tmp_path`` plus a stubbed fastembed (so we
don't pull 2 GB of model data on every CI run). The embedding provider
returns deterministic vectors keyed off the input text so search
results are predictable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest

from durin.memory.embedding import EmbeddingProvider
from durin.memory.store import store_memory
from durin.memory.vector_index import VectorIndex, vector_index_available


class _FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic embeddings keyed off the first character of the text.

    Vectors are 8-dim so the records stay small. The first dimension is
    derived from the input — texts starting with the same character
    embed to identical vectors, which lets the tests assert that a
    query for one text retrieves the closest stored text.
    """

    DIM = 8

    @property
    def model_name(self) -> str:
        return "fake/test-embed"

    @property
    def dimensions(self) -> int:
        return self.DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            seed = float(ord(text[0])) if text else 0.0
            out.append([seed] + [0.0] * (self.DIM - 1))
        return out


@pytest.fixture
def provider() -> _FakeEmbeddingProvider:
    return _FakeEmbeddingProvider()


pytestmark = pytest.mark.skipif(
    not vector_index_available(),
    reason="lancedb is not installed; install durin[memory] to run these tests",
)


# ---------------------------------------------------------------------------
# upsert / search
# ---------------------------------------------------------------------------


def test_upsert_creates_table_and_returns_via_search(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    workspace = tmp_path
    result = store_memory(workspace, content="alpha body", headline="alpha")
    entry_path = Path(result["path"])
    from durin.memory.storage import load_entry

    index = VectorIndex(workspace, provider)
    index.upsert(load_entry(entry_path), result["class"], entry_path)

    hits = index.search("alpha query", top_k=5)
    assert hits
    assert hits[0]["headline"] == "alpha"


def test_upsert_is_idempotent_on_id(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    """Re-upserting the same id replaces the prior row, doesn't duplicate."""
    workspace = tmp_path
    result = store_memory(workspace, content="content one", headline="first")
    entry_path = Path(result["path"])
    from durin.memory.storage import load_entry

    index = VectorIndex(workspace, provider)
    entry = load_entry(entry_path)
    index.upsert(entry, result["class"], entry_path)
    index.upsert(entry, result["class"], entry_path)

    hits = index.search("content", top_k=10)
    assert sum(1 for h in hits if h["id"] == entry.id) == 1


def test_search_returns_top_k_by_similarity(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    workspace = tmp_path
    items = [
        ("alpha body content", "alpha-headline"),
        ("beta body content", "beta-headline"),
        ("gamma body content", "gamma-headline"),
    ]
    index = VectorIndex(workspace, provider)
    from durin.memory.storage import load_entry

    for content, headline in items:
        r = store_memory(workspace, content=content, headline=headline)
        index.upsert(load_entry(Path(r["path"])), r["class"], Path(r["path"]))

    # Query starting with 'g' should pull the 'gamma' record first
    # because the fake provider keys off the first character.
    hits = index.search("gamma question", top_k=1)
    assert len(hits) == 1
    assert hits[0]["headline"] == "gamma-headline"


def test_search_empty_query_returns_empty(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    index = VectorIndex(tmp_path, provider)
    assert index.search("") == []
    assert index.search("   ") == []
    assert index.search("anything", top_k=0) == []


def test_search_missing_table_returns_empty(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    """No upserts yet → search returns []."""
    index = VectorIndex(tmp_path, provider)
    assert index.search("query") == []


def test_search_strips_vector_from_results(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    workspace = tmp_path
    r = store_memory(workspace, content="x", headline="h")
    from durin.memory.storage import load_entry

    index = VectorIndex(workspace, provider)
    index.upsert(load_entry(Path(r["path"])), r["class"], Path(r["path"]))

    hits = index.search("query")
    for hit in hits:
        assert "vector" not in hit, "raw vector column must not leak to callers"


# ---------------------------------------------------------------------------
# rebuild_from_workspace
# ---------------------------------------------------------------------------


def test_rebuild_walks_all_classes(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    workspace = tmp_path
    store_memory(workspace, content="a", headline="A", class_name="stable")
    store_memory(workspace, content="b", headline="B", class_name="episodic")
    store_memory(workspace, content="c", headline="C", class_name="corpus")

    index = VectorIndex(workspace, provider)
    count = index.rebuild_from_workspace()
    assert count == 3

    hits = index.search("query", top_k=10)
    assert len(hits) == 3
    headlines = {h["headline"] for h in hits}
    assert headlines == {"A", "B", "C"}


def test_rebuild_drops_stale_entries(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    """Rebuild rebuilds: stale entries from a prior rebuild are gone."""
    workspace = tmp_path
    r1 = store_memory(workspace, content="first", headline="FIRST")
    from durin.memory.storage import load_entry

    index = VectorIndex(workspace, provider)
    index.upsert(load_entry(Path(r1["path"])), r1["class"], Path(r1["path"]))

    # Now remove the on-disk markdown file but keep the prior index row.
    Path(r1["path"]).unlink()
    assert not Path(r1["path"]).is_file()

    count = index.rebuild_from_workspace()
    assert count == 0
    assert index.search("first") == []


def test_rebuild_empty_workspace_drops_table(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    index = VectorIndex(tmp_path, provider)
    count = index.rebuild_from_workspace()
    assert count == 0
    assert index.search("anything") == []


def test_rebuild_skips_malformed_files(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    """A broken file must not crash the rebuild — it's just skipped."""
    workspace = tmp_path
    store_memory(workspace, content="good", headline="GOOD")
    # Drop a malformed file alongside the good one.
    bad_dir = workspace / "memory" / "stable"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "broken.md").write_text("no frontmatter here\n", encoding="utf-8")

    index = VectorIndex(workspace, provider)
    count = index.rebuild_from_workspace()
    assert count == 1


# ---------------------------------------------------------------------------
# embed_text selection rule
# ---------------------------------------------------------------------------


def test_embed_text_prefers_summary_then_headline_then_body() -> None:
    from durin.memory.schema import MemoryEntry

    full = MemoryEntry(id="x", headline="hed", summary="sum", body="bod")
    assert VectorIndex._embed_text(full) == "sum"

    no_summary = MemoryEntry(id="x", headline="hed", body="bod")
    assert VectorIndex._embed_text(no_summary) == "hed"

    body_only = MemoryEntry(id="x", headline=" ", body="bod")
    assert VectorIndex._embed_text(body_only) == "bod"
