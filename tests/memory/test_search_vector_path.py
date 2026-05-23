"""Tests for the wired vector path in MemoryStoreTool + MemorySearchTool.

memory_store now upserts each new entry into the VectorIndex; memory_search
prefers the vector index for warm-tier dreamed queries with grep as
fallback. The lazy VectorIndex construction inside each tool depends on
both lancedb being available AND an embedding model name being passed in
(``embedding_model`` kw).

These tests stub fastembed via ``sys.modules`` so we don't pull the real
2 GB model; lancedb itself runs against a real on-disk DB in ``tmp_path``.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

from durin.memory.vector_index import vector_index_available


pytestmark = pytest.mark.skipif(
    not vector_index_available(),
    reason="lancedb is not installed; install durin[memory] to run these tests",
)


_TEST_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# What the fake fastembed pretends its catalog says. The model id
# matches durin's real default; the stub dim (8) is small for speed.
_STUB_CATALOG = [{"model": _TEST_MODEL, "dim": 8, "size_in_GB": 0.22}]


class _FakeTextEmbedding:
    """Deterministic stub for fastembed.TextEmbedding."""

    @staticmethod
    def list_supported_models():
        return list(_STUB_CATALOG)

    def __init__(self, model_name=None, **_):
        self.model_name = model_name

    def embed(self, texts):
        # Embed by first character so search results are predictable.
        for text in texts:
            seed = float(ord(text[0])) if text else 0.0
            yield [seed] + [0.0] * 7


@contextmanager
def _stub_fastembed():
    import durin.memory.embedding as embedding_module

    embedding_module._CATALOG_CACHE = None
    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
    sys.modules["fastembed"] = fake
    try:
        yield
    finally:
        sys.modules.pop("fastembed", None)
        embedding_module._CATALOG_CACHE = None


# ---------------------------------------------------------------------------
# memory_store wiring (upsert after write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_upserts_into_vector_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from durin.agent.tools.memory_store import MemoryStoreTool
    from durin.memory.vector_index import VectorIndex
    from durin.memory.embedding import FastembedProvider

    with _stub_fastembed():
        tool = MemoryStoreTool(
            workspace=tmp_path,
            embedding_model=_TEST_MODEL,
        )
        out = await tool.execute(
            content="cache must be flushed when payload version changes",
            headline="cache flush rule",
        )

        # Index contains the entry
        vi = VectorIndex(tmp_path, FastembedProvider(_TEST_MODEL))
        hits = vi.search("cache", top_k=5)

    assert "error" not in out
    assert any(h["id"] == out["id"] for h in hits)


@pytest.mark.asyncio
async def test_store_without_embedding_model_skips_vector(
    tmp_path: Path,
) -> None:
    """No embedding_model → tool stays grep-only; store still succeeds."""
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)  # no embedding_model
    out = await tool.execute(content="content", headline="h")
    assert "error" not in out
    # No vector index folder created on disk
    assert not (tmp_path / "memory" / ".index.lance").exists()


@pytest.mark.asyncio
async def test_store_vector_failure_does_not_break_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vector upsert failure must still let the markdown write succeed."""
    from durin.agent.tools.memory_store import MemoryStoreTool
    from durin.memory.storage import load_entry

    # Force fastembed import to fail inside the upsert path.
    monkeypatch.setitem(sys.modules, "fastembed", None)
    tool = MemoryStoreTool(
        workspace=tmp_path,
        embedding_model=_TEST_MODEL,
    )
    out = await tool.execute(content="content", headline="h")
    assert "error" not in out
    # Markdown still written
    entry = load_entry(Path(out["path"]))
    assert entry.headline == "h"


# ---------------------------------------------------------------------------
# memory_search wiring (vector path + fallback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_uses_vector_for_dreamed_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.agent.tools.memory_store import MemoryStoreTool

    with _stub_fastembed():
        store = MemoryStoreTool(
            workspace=tmp_path,
            embedding_model=_TEST_MODEL,
        )
        await store.execute(content="alpha content", headline="alpha")
        await store.execute(content="beta content", headline="beta")

        search = MemorySearchTool(
            workspace=tmp_path,
            embedding_model=_TEST_MODEL,
        )
        out = await search.execute(query="alpha", scope="dreamed", level="warm")

    assert out["strategy"] == "vector"
    assert out["total"] >= 1
    # The 'alpha' query (first char 'a') matches the alpha entry
    headlines = {r["headline"] for r in out["results"]}
    assert "alpha" in headlines


@pytest.mark.asyncio
async def test_search_fallback_to_grep_when_vector_unavailable(
    tmp_path: Path,
) -> None:
    """Without embedding_model the tool must still return grep results."""
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.store import store_memory

    store_memory(tmp_path, content="cache layer learning", headline="cache")

    tool = MemorySearchTool(workspace=tmp_path)  # no embedding_model
    out = await tool.execute(query="cache", scope="dreamed", level="warm")
    assert out["strategy"] == "grep"
    assert out["total"] >= 1


@pytest.mark.asyncio
async def test_search_scope_all_combines_vector_and_grep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.agent.tools.memory_store import MemoryStoreTool

    # Prep: one stored memory entry (will be in vector index) + one
    # session.md grep-able for the same query token.
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "s1.md").write_text(
        "# s\n\n## turn-1\nalpha session mention\n", encoding="utf-8"
    )

    with _stub_fastembed():
        await MemoryStoreTool(
            workspace=tmp_path,
            embedding_model=_TEST_MODEL,
        ).execute(content="alpha memory body", headline="alpha-memory")

        search = MemorySearchTool(
            workspace=tmp_path,
            embedding_model=_TEST_MODEL,
        )
        out = await search.execute(query="alpha", scope="all", level="warm")

    assert out["strategy"] == "hybrid"
    sources = {r["source"] for r in out["results"]}
    assert "memory" in sources   # from vector
    assert "sessions" in sources  # from grep


@pytest.mark.asyncio
async def test_search_cold_level_uses_grep_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """level=cold needs full bodies → grep handles it, vector skipped."""
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.agent.tools.memory_store import MemoryStoreTool

    with _stub_fastembed():
        await MemoryStoreTool(
            workspace=tmp_path,
            embedding_model=_TEST_MODEL,
        ).execute(content="alpha cold body", headline="alpha")

        search = MemorySearchTool(
            workspace=tmp_path,
            embedding_model=_TEST_MODEL,
        )
        out = await search.execute(query="alpha", scope="dreamed", level="cold")

    assert out["strategy"] == "grep"
    assert out["total"] >= 1
    # Cold tier returns bodies
    assert any(r.get("body") for r in out["results"])
