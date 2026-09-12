"""VectorIndex.entity_page_vectors / embed_passages — the surface the dream's
semantic walk reads instead of re-embedding every page (requires lancedb)."""
from pathlib import Path

import pytest

pytest.importorskip("lancedb")

from durin.memory.embedding import EmbeddingProvider
from durin.memory.schema import MemoryEntry
from durin.memory.vector_index import VectorIndex


class _Provider(EmbeddingProvider):
    DIM = 4

    @property
    def model_name(self) -> str:
        return "fake/x"

    @property
    def dimensions(self) -> int:
        return self.DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t) % 7), 1.0, 0.0, 0.0] for t in texts]


def _page(index: VectorIndex, ws: Path, ref: str, name: str) -> None:
    t, slug = ref.split(":", 1)
    path = ws / "memory" / "entities" / t / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: {t}\nname: {name}\n---\n{name} body\n")
    index.upsert_entity_page(entity_ref=ref, name=name, aliases=[], body=f"{name} body", path=path)


def test_entity_page_vectors_returns_every_entity_row_and_nothing_else(tmp_path):
    index = VectorIndex(tmp_path, _Provider())
    assert index.entity_page_vectors() == {}  # no table yet
    _page(index, tmp_path, "person:ana", "Ana")
    _page(index, tmp_path, "project:durin", "Durin")
    entry = MemoryEntry(id="m1", headline="a fact", summary="a fact", body="durin is a fact")
    index.upsert(entry, "fact", tmp_path / "memory" / "fact" / "m1.md")
    vectors = index.entity_page_vectors()
    assert set(vectors) == {"person:ana", "project:durin"}
    assert all(len(v) == _Provider.DIM and all(isinstance(x, float) for x in v) for v in vectors.values())
    # the stored vector is the passage embedding of the page's composed text
    [fresh] = index.embed_passages([VectorIndex._compose_entity_page_text(
        name="Ana", aliases=[], body="Ana body", attributes={}, relations=[])])
    assert fresh == vectors["person:ana"]


def test_embed_passages_empty_is_a_noop(tmp_path):
    assert VectorIndex(tmp_path, _Provider()).embed_passages([]) == []
