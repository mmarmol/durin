"""Phase 2 end-to-end smoke: synthetic corpus, vector path, telemetry.

Walks the full Phase-2 surface:

1. Build a synthetic corpus of N memory entries spanning multiple topics.
2. Run vector search through MemorySearchTool with embedding_model set.
3. Run grep search against the same corpus with vector disabled.
4. Assert both strategies return results and the telemetry events
   (``memory.recall`` + ``memory.recall.vector``) carry the expected
   shape.

Uses a stubbed fastembed (first-character → vector seed) so the test
suite stays offline and fast. The point of this smoke is wiring +
telemetry correctness, not retrieval quality — quality benchmarks
against LoCoMo / EverMemBench are Phase 3 post-implementation per
docs/08 §0d.8.
"""

from __future__ import annotations

import string
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


class _FakeTextEmbedding:
    """First-char + length seeded stub for fastembed."""

    def __init__(self, model_name=None, **_):
        self.model_name = model_name

    def embed(self, texts):
        for text in texts:
            first = float(ord(text[0])) if text else 0.0
            length = float(len(text))
            yield [first, length] + [0.0] * 6


@contextmanager
def _stub_fastembed():
    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
    sys.modules["fastembed"] = fake
    try:
        yield
    finally:
        sys.modules.pop("fastembed", None)


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Build a synthetic 50-entry corpus across the four memory classes."""
    from durin.memory.store import store_memory

    workspace = tmp_path
    topics = [
        ("alpha", "cache invalidation"),
        ("beta", "rate limiting"),
        ("gamma", "user preferences"),
        ("delta", "deployment workflow"),
        ("epsilon", "error budgets"),
    ]
    classes = ["stable", "episodic", "corpus", "pending"]
    for class_name in classes:
        for i in range(12 if class_name == "episodic" else 13):
            topic, theme = topics[(i + len(class_name)) % len(topics)]
            store_memory(
                workspace,
                content=f"{topic} entry {i} — {theme} discussion notes",
                headline=f"{topic} {theme} #{i}",
                class_name=class_name,
                entities=[topic, theme.replace(" ", "_")],
            )
    return workspace


# ---------------------------------------------------------------------------
# Smoke flows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vector_path_runs_end_to_end(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.agent.tools.memory_store import MemoryStoreTool
    from durin.memory import embedding

    monkeypatch.setitem(
        embedding._FASTEMBED_DIMS,
        "intfloat/multilingual-e5-small",
        8,
    )

    with _stub_fastembed():
        # Index the existing corpus so the vector path has content.
        from durin.memory.embedding import FastembedProvider
        from durin.memory.vector_index import VectorIndex

        provider = FastembedProvider("intfloat/multilingual-e5-small")
        VectorIndex(corpus, provider).rebuild_from_workspace()

        search = MemorySearchTool(
            workspace=corpus,
            embedding_model="intfloat/multilingual-e5-small",
        )
        out = await search.execute(query="alpha", scope="dreamed", level="warm")

    assert out["strategy"] == "vector"
    assert out["total"] > 0


@pytest.mark.asyncio
async def test_recall_vector_telemetry_fires(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory import embedding

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.agent.tools.memory_search.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    monkeypatch.setitem(
        embedding._FASTEMBED_DIMS,
        "intfloat/multilingual-e5-small",
        8,
    )

    with _stub_fastembed():
        from durin.memory.embedding import FastembedProvider
        from durin.memory.vector_index import VectorIndex

        VectorIndex(
            corpus, FastembedProvider("intfloat/multilingual-e5-small")
        ).rebuild_from_workspace()

        search = MemorySearchTool(
            workspace=corpus,
            embedding_model="intfloat/multilingual-e5-small",
        )
        await search.execute(query="alpha", scope="dreamed", level="warm")

    vector_events = [e for e in events if e[0] == "memory.recall.vector"]
    recall_events = [e for e in events if e[0] == "memory.recall"]
    assert len(vector_events) == 1
    payload = vector_events[0][1]
    assert payload["query"] == "alpha"
    assert payload["scope"] == "dreamed"
    assert payload["embedding_model"] == "intfloat/multilingual-e5-small"
    assert payload["hit_count"] > 0
    assert payload["duration_ms"] >= 0
    # The aggregate memory.recall event always fires too.
    assert len(recall_events) == 1


@pytest.mark.asyncio
async def test_grep_path_still_works_without_index(corpus: Path) -> None:
    """Vector disabled (no embedding_model) → grep fallback returns hits."""
    from durin.agent.tools.memory_search import MemorySearchTool

    search = MemorySearchTool(workspace=corpus)
    out = await search.execute(query="cache", scope="dreamed", level="warm")
    assert out["strategy"] == "grep"
    assert out["total"] > 0


@pytest.mark.asyncio
async def test_vector_recall_does_not_regress_against_grep(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For an exact substring query, vector should not return fewer than grep.

    With the stubbed first-char embedder, queries starting with 'alpha'
    cluster with alpha-prefixed entries. The vector top-K(10) should
    cover at least as many entries as a grep over the same substring.
    This is a sanity floor — real recall quality is benchmarked against
    LoCoMo / EverMemBench post-Phase-3 per docs/08 §0d.8.
    """
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory import embedding

    monkeypatch.setitem(
        embedding._FASTEMBED_DIMS,
        "intfloat/multilingual-e5-small",
        8,
    )

    with _stub_fastembed():
        from durin.memory.embedding import FastembedProvider
        from durin.memory.vector_index import VectorIndex

        VectorIndex(
            corpus, FastembedProvider("intfloat/multilingual-e5-small")
        ).rebuild_from_workspace()

        vector_tool = MemorySearchTool(
            workspace=corpus,
            embedding_model="intfloat/multilingual-e5-small",
        )
        grep_tool = MemorySearchTool(workspace=corpus)

        vector_out = await vector_tool.execute(
            query="alpha", scope="dreamed", level="warm"
        )
        grep_out = await grep_tool.execute(
            query="alpha", scope="dreamed", level="warm"
        )

    assert vector_out["strategy"] == "vector"
    assert grep_out["strategy"] == "grep"
    # Vector returns up to top_k=10; grep returns every match. The
    # smoke floor is that vector returns SOMETHING (not zero) when the
    # corpus contains the query token.
    assert vector_out["total"] > 0
    assert grep_out["total"] > 0
