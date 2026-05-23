"""Tests for memory entry storage (durin.memory.store + MemoryStoreTool)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from durin.memory.provenance import author_scope
from durin.memory.store import StoreError, store_memory
from durin.memory.storage import load_entry


# ---------------------------------------------------------------------------
# store_memory (pure function)
# ---------------------------------------------------------------------------


def test_store_minimal_entry_writes_to_episodic_by_default(tmp_path: Path) -> None:
    result = store_memory(tmp_path, content="A learning worth keeping.")
    assert result["class"] == "episodic"
    path = Path(result["path"])
    assert path.is_file()
    assert path.parent == tmp_path / "memory" / "episodic"
    loaded = load_entry(path)
    assert loaded.body == "A learning worth keeping."
    assert loaded.headline.startswith("A learning")


def test_store_auto_headline_from_first_words(tmp_path: Path) -> None:
    body = (
        "User prefiere terse responses sin emojis a menos que los pida "
        "explicitamente."
    )
    result = store_memory(tmp_path, content=body)
    # First 10 words by default
    expected = " ".join(body.split()[:10])
    assert result["headline"] == expected


def test_store_explicit_headline_wins(tmp_path: Path) -> None:
    result = store_memory(
        tmp_path,
        content="long body text here",
        headline="my custom headline",
    )
    assert result["headline"] == "my custom headline"


def test_store_idempotent_for_same_class_and_content(tmp_path: Path) -> None:
    first = store_memory(tmp_path, content="identical body")
    second = store_memory(tmp_path, content="identical body")
    assert first["id"] == second["id"]
    assert first["path"] == second["path"]


def test_store_distinct_classes_get_distinct_ids(tmp_path: Path) -> None:
    first = store_memory(tmp_path, content="same body", class_name="episodic")
    second = store_memory(tmp_path, content="same body", class_name="stable")
    assert first["id"] != second["id"]


def test_store_records_full_frontmatter(tmp_path: Path) -> None:
    result = store_memory(
        tmp_path,
        content="Body of the entry.",
        class_name="stable",
        headline="explicit headline",
        summary="explicit summary",
        source_refs=["[turn 42](../sessions/abc.md#turn-42)"],
        entities=["person:marcelo", "project:durin"],
        valid_from=date(2026, 5, 20),
    )
    loaded = load_entry(Path(result["path"]))
    assert loaded.headline == "explicit headline"
    assert loaded.summary == "explicit summary"
    assert loaded.source_refs == ["[turn 42](../sessions/abc.md#turn-42)"]
    assert loaded.entities == ["person:marcelo", "project:durin"]
    assert loaded.valid_from == date(2026, 5, 20)
    assert loaded.body == "Body of the entry."


def test_store_uses_current_author_by_default(tmp_path: Path) -> None:
    """Without an author_scope, author defaults to user_authored."""
    result = store_memory(tmp_path, content="hand-edited content")
    loaded = load_entry(Path(result["path"]))
    assert loaded.author == "user_authored"


def test_store_inside_agent_scope_marks_agent_created(tmp_path: Path) -> None:
    with author_scope("agent_created"):
        result = store_memory(tmp_path, content="agent-derived content")
    loaded = load_entry(Path(result["path"]))
    assert loaded.author == "agent_created"


def test_store_rejects_empty_content(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="content"):
        store_memory(tmp_path, content="")
    with pytest.raises(StoreError, match="content"):
        store_memory(tmp_path, content="   \n  \t  ")


def test_store_rejects_unknown_class(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="unknown class"):
        store_memory(tmp_path, content="body", class_name="feedback")


# ---------------------------------------------------------------------------
# MemoryStoreTool (agent-facing wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_happy_path_marks_agent_created(tmp_path: Path) -> None:
    """Tool implicitly applies author_scope('agent_created')."""
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)
    out = await tool.execute(content="Something the agent learned.")
    assert "error" not in out
    assert out["author"] == "agent_created"
    loaded = load_entry(Path(out["path"]))
    assert loaded.author == "agent_created"


@pytest.mark.asyncio
async def test_tool_error_on_empty_content(tmp_path: Path) -> None:
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)
    out = await tool.execute(content="")
    assert out == {"error": "content is required"}


@pytest.mark.asyncio
async def test_tool_threads_optional_metadata(tmp_path: Path) -> None:
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)
    out = await tool.execute(
        content="body",
        class_name="stable",
        headline="explicit",
        summary="s",
        source_refs=["[t1](../sessions/x.md#turn-1)"],
        entities=["topic:e1", "topic:e2"],
    )
    loaded = load_entry(Path(out["path"]))
    assert loaded.headline == "explicit"
    assert loaded.summary == "s"
    assert loaded.source_refs == ["[t1](../sessions/x.md#turn-1)"]
    assert loaded.entities == ["topic:e1", "topic:e2"]


# ---------------------------------------------------------------------------
# Dedup pre-persist (T1.7 + G1 threshold + G5 cached embed + G6 force)
# ---------------------------------------------------------------------------


class _StubVectorIndex:
    """Minimal stub of VectorIndex for dedup tests — no LanceDB needed.

    Stores (id, vector, headline) tuples. embed_text returns a vector
    based on content hash (deterministic). search_by_vector returns
    the existing entries with computed L2 distances.
    """

    def __init__(self):
        self.entries: list[tuple[str, list[float], str]] = []
        self.upsert_calls = 0
        self.upsert_with_vector_calls = 0

    def embed_text(self, text: str) -> list[float]:
        # Deterministic embedding by character bucketing into 8 dims.
        vec = [0.0] * 8
        for ch in text.lower():
            if ch.isalpha():
                vec[(ord(ch) - ord("a")) % 8] += 1.0
        # Normalize to unit (so L2² = 2(1-cos))
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    def search_by_vector(self, vec, *, top_k=10):
        results = []
        for id_, ev, hl in self.entries:
            # L2 squared distance
            d = sum((a - b) ** 2 for a, b in zip(vec, ev))
            results.append({"id": id_, "_distance": d, "headline": hl})
        results.sort(key=lambda r: r["_distance"])
        return results[:top_k]

    def upsert(self, entry, class_name, path):
        self.upsert_calls += 1
        v = self.embed_text(entry.body)
        self.entries.append((entry.id, v, entry.headline))

    def upsert_with_vector(self, entry, class_name, path, *, precomputed_vector):
        self.upsert_with_vector_calls += 1
        self.entries.append((entry.id, precomputed_vector, entry.headline))


@pytest.mark.asyncio
async def test_dedup_warning_on_near_duplicate(tmp_path: Path) -> None:
    """Second write of nearly-identical content returns warning, not result."""
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)
    stub = _StubVectorIndex()
    tool._vector_index = stub
    tool._vector_index_attempted = True

    # First write succeeds
    first = await tool.execute(content="Marcelo prefers pytest over unittest.")
    assert "error" not in first
    assert "warning" not in first
    assert first["id"]

    # Second write with identical content → warning
    second = await tool.execute(content="Marcelo prefers pytest over unittest.")
    assert second.get("warning") == "near-duplicate"
    assert second["nearest_id"] == first["id"]
    assert "force=true" in second["hint"]


@pytest.mark.asyncio
async def test_dedup_force_overrides(tmp_path: Path) -> None:
    """force=true skips the dedup check and writes anyway."""
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)
    stub = _StubVectorIndex()
    tool._vector_index = stub
    tool._vector_index_attempted = True

    first = await tool.execute(content="duplicate content")
    second = await tool.execute(content="duplicate content", force=True)
    # Second should NOT be a warning — force bypasses dedup
    assert "warning" not in second
    assert second["id"]


@pytest.mark.asyncio
async def test_dedup_allows_distinct_content(tmp_path: Path) -> None:
    """Different content does NOT trigger warning."""
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)
    stub = _StubVectorIndex()
    tool._vector_index = stub
    tool._vector_index_attempted = True

    await tool.execute(content="qqqqqqqqqq qqqqqq qqq qqq")
    second = await tool.execute(content="zzzzzzz fff bbbb")
    assert "warning" not in second
    assert second["id"]


@pytest.mark.asyncio
async def test_dedup_cached_embedding_avoids_double_compute(tmp_path: Path) -> None:
    """G5: same embedding used for dedup check AND upsert (no recompute).

    Verifies that when dedup runs (vi is enabled, force=false), the
    upsert path uses upsert_with_vector (which takes precomputed
    vector) instead of upsert (which would recompute internally).
    """
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)
    stub = _StubVectorIndex()
    tool._vector_index = stub
    tool._vector_index_attempted = True

    await tool.execute(content="fresh content for dedup path")
    # First write: should use the cached-vector path
    assert stub.upsert_with_vector_calls == 1
    assert stub.upsert_calls == 0


@pytest.mark.asyncio
async def test_dedup_check_failure_does_not_block_write(tmp_path: Path) -> None:
    """If the vector index raises during dedup, the write still proceeds."""
    from durin.agent.tools.memory_store import MemoryStoreTool

    class _RaisingVectorIndex:
        def embed_text(self, text):
            raise RuntimeError("simulated embedding failure")

        def search_by_vector(self, *args, **kwargs):
            raise RuntimeError("should not be called")

        def upsert(self, *args, **kwargs):
            pass

        def upsert_with_vector(self, *args, **kwargs):
            pass

    tool = MemoryStoreTool(workspace=tmp_path)
    tool._vector_index = _RaisingVectorIndex()
    tool._vector_index_attempted = True

    out = await tool.execute(content="content despite failure")
    # Write should succeed (markdown is source of truth)
    assert "error" not in out
    assert "warning" not in out
    assert out["id"]
