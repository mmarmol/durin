"""Tests for skill indexing in the LanceDB-backed vector index.

Skills become ``class_name="skill"`` rows in the SAME lance table as
memory entries and entity pages. Mirrors the entity-page machinery
(``upsert_entity_page`` / rebuild Pass 2). Uses the same fake
embedding provider as ``test_vector_index.py`` so search results stay
deterministic and CI doesn't pull the real fastembed model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("lancedb")

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


def test_upsert_skill_then_search_finds_it(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    vi = VectorIndex(tmp_path, provider)
    vi.upsert_skill(
        name="git-helper",
        description="git rebase flow steps",
        body="run git rebase -i then fixup",
        path="skills/git-helper/SKILL.md",
    )
    hits = vi.search("git rebase", top_k=5)
    ids = [h["id"] for h in hits]
    assert "skill/git-helper" in ids
    row = next(h for h in hits if h["id"] == "skill/git-helper")
    assert row["class_name"] == "skill"
    assert row["path"] == "skills/git-helper/SKILL.md"


def test_delete_by_id_removes_skill(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    vi = VectorIndex(tmp_path, provider)
    vi.upsert_skill(name="x", description="d", body="b", path="skills/x/SKILL.md")
    assert vi.delete_by_id("skill/x") is True
    assert all(h["id"] != "skill/x" for h in vi.search("d", top_k=5))


def test_rebuild_includes_skills(
    tmp_path: Path, provider: _FakeEmbeddingProvider
) -> None:
    d = tmp_path / "skills" / "deploy"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: deploy flow\n---\nkubectl apply\n",
        encoding="utf-8",
    )
    vi = VectorIndex(tmp_path, provider)
    vi.rebuild_from_workspace()
    assert any(h["id"] == "skill/deploy" for h in vi.search("deploy flow", top_k=5))
