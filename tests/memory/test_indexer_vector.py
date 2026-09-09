"""Tests for the entry branch of ``reindex_one_file_vector``.

The reactive index path (file watcher) already embedded entity pages;
this covers the other half — ``memory/<class>/<id>.md`` entries
(episodic, stable, corpus, session_summary). Uses the same fake
embedding provider as ``test_vector_index.py`` so search results stay
deterministic and CI doesn't pull the real fastembed model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.embedding import EmbeddingProvider
from durin.memory.vector_index import VectorIndex, vector_index_available


class _FakeEmbeddingProvider(EmbeddingProvider):
    """Deterministic embeddings keyed off the first character of the text.

    Identical to the provider in ``test_vector_index.py``: 8-dim vectors,
    first dimension derived from the input's first character, so a query
    sharing its first char with a stored text retrieves it.
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


def test_an_episodic_entry_is_embedded_by_the_reactive_path(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    from durin.memory.indexer import reindex_one_file_vector
    from durin.memory.store import store_memory

    ws = tmp_path / "ws"
    result = store_memory(
        ws, content="Bruenor prefiere el hacha de doble filo.", class_name="episodic"
    )
    vi = VectorIndex(ws, provider)
    assert reindex_one_file_vector(ws, Path(result["path"]), vi) is True
    rows = vi.search("hacha", top_k=3)
    assert rows and rows[0]["class_name"] == "episodic"
    assert rows[0]["id"] == result["id"]


def test_a_session_summary_is_embedded_too(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    from durin.memory.indexer import reindex_one_file_vector
    from durin.memory.session_summary_store import write_session_summary
    from durin.memory.storage import load_entry

    ws = tmp_path / "ws"
    path = write_session_summary(
        ws, "session-1", "Bruenor discute su colección de hachas en detalle."
    )
    assert path is not None
    vi = VectorIndex(ws, provider)
    assert reindex_one_file_vector(ws, path, vi) is True
    rows = vi.search("hachas", top_k=3)
    assert rows and rows[0]["class_name"] == "session_summary"
    assert rows[0]["id"] == load_entry(path).id
