"""Tests for the wired vector path in MemorySearchTool.

Memory entries land in the VectorIndex via ``store_memory`` (the write) plus
an explicit ``VectorIndex.upsert`` (the index side-effect that a live write
path — ``/remember``, ``memory_upsert_entity``, the dream — performs after
the file write); ``memory_search`` prefers the vector index for warm-tier
dreamed queries with grep as fallback. The lazy VectorIndex construction
inside the search tool depends on both lancedb being available AND an
embedding model name being passed in (``embedding_model`` kw).

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

    @staticmethod
    def add_custom_model(**_kwargs) -> None:
        # No-op: production `_register_custom_models()` calls this on the
        # real fastembed. The stub catalog already covers the model, so we
        # skip the side effect. Without this the test is order-dependent —
        # it only passes when a prior test (e.g. test_embedding) populated
        # the module-level `_REGISTERED_CUSTOM` set first.
        pass

    def __init__(self, model_name=None, **_):
        self.model_name = model_name

    def embed(self, texts, batch_size=256, **_kwargs):
        # Embed by first character so search results are predictable.
        for text in texts:
            seed = float(ord(text[0])) if text else 0.0
            yield [seed] + [0.0] * 7


@contextmanager
def _stub_fastembed():
    import durin.memory.embedding as embedding_module
    from durin.config import loader as config_loader

    embedding_module._CATALOG_CACHE = None
    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
    sys.modules["fastembed"] = fake

    # VectorIndex / MemorySearchTool build their provider via
    # provider_from_config(load_config(), ...), which defaults isolation to
    # "process" — a real subprocess that doesn't see this sys.modules stub
    # (separate process) and would embed with the real fastembed model
    # instead. Force inline so the fake stays authoritative end-to-end.
    real_load_config = config_loader.load_config

    def _inline_load_config(*args, **kwargs):
        cfg = real_load_config(*args, **kwargs)
        cfg.memory.embedding.isolation = "inline"
        return cfg

    config_loader.load_config = _inline_load_config
    try:
        yield
    finally:
        sys.modules.pop("fastembed", None)
        embedding_module._CATALOG_CACHE = None
        config_loader.load_config = real_load_config


# ---------------------------------------------------------------------------
# memory_search wiring (vector path + fallback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_uses_vector_for_dreamed_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.embedding import FastembedProvider
    from durin.memory.storage import load_entry
    from durin.memory.store import store_memory
    from durin.memory.vector_index import VectorIndex

    with _stub_fastembed():
        vi = VectorIndex(tmp_path, FastembedProvider(_TEST_MODEL))
        for content, headline in (
            ("alpha content", "alpha"),
            ("beta content", "beta"),
        ):
            stored = store_memory(tmp_path, content=content, headline=headline)
            vi.upsert(
                load_entry(Path(stored["path"])), stored["class"], Path(stored["path"])
            )

        search = MemorySearchTool(
            workspace=tmp_path,
            embedding_model=_TEST_MODEL,
        )
        out = await search.execute(query="alpha", scope="dreamed", level="warm")

    # v2 pipeline runs vector + lexical + grep concurrently; the
    # strategy label reflects which sources contributed hits.
    assert out["strategy"] in ("vector", "hybrid", "lexical")
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
    # v2 pipeline: when no vector + no FTS, the grep fallback carries.
    assert out["strategy"] in ("grep", "lexical")
    assert out["total"] >= 1


@pytest.mark.asyncio
async def test_search_scope_all_combines_vector_and_grep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.embedding import FastembedProvider
    from durin.memory.storage import load_entry
    from durin.memory.store import store_memory
    from durin.memory.vector_index import VectorIndex

    # Prep: one stored memory entry (will be in vector index) + one
    # session.md grep-able for the same query token.
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "s1.md").write_text(
        "# s\n\n## turn-1\nalpha session mention\n", encoding="utf-8"
    )

    with _stub_fastembed():
        vi = VectorIndex(tmp_path, FastembedProvider(_TEST_MODEL))
        stored = store_memory(tmp_path, content="alpha memory body", headline="alpha-memory")
        vi.upsert(
            load_entry(Path(stored["path"])), stored["class"], Path(stored["path"])
        )

        search = MemorySearchTool(
            workspace=tmp_path,
            embedding_model=_TEST_MODEL,
        )
        out = await search.execute(query="alpha", scope="all", level="warm")

    assert out["strategy"] in ("hybrid", "vector", "lexical")
    sources = {r["source"] for r in out["results"]}
    assert "memory" in sources   # from vector
    assert "sessions" in sources  # from grep


@pytest.mark.asyncio
async def test_search_cold_level_uses_vector_with_body_enrichment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """level=cold goes through vector and enriches each hit with the
    body from disk. Earlier versions short-circuited cold to literal
    substring grep, which failed for any natural-language query that
    didn't appear verbatim in the entry."""
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.embedding import FastembedProvider
    from durin.memory.storage import load_entry
    from durin.memory.store import store_memory
    from durin.memory.vector_index import VectorIndex

    with _stub_fastembed():
        vi = VectorIndex(tmp_path, FastembedProvider(_TEST_MODEL))
        stored = store_memory(tmp_path, content="alpha cold body", headline="alpha")
        vi.upsert(
            load_entry(Path(stored["path"])), stored["class"], Path(stored["path"])
        )

        search = MemorySearchTool(
            workspace=tmp_path,
            embedding_model=_TEST_MODEL,
        )
        out = await search.execute(query="alpha", scope="dreamed", level="cold")

    # v2 pipeline runs vector + lexical + grep concurrently; the
    # strategy label reflects which sources contributed hits.
    assert out["strategy"] in ("vector", "hybrid", "lexical")
    assert out["total"] >= 1
    # Cold tier returns bodies — populated from disk after vector hit.
    assert any(r.get("body") for r in out["results"])


@pytest.mark.asyncio
async def test_search_scope_library_isolates_reference_via_vector_index(
    tmp_path: Path,
) -> None:
    """The scope predicate must reach the REAL vector index as a `where`
    prefilter: `scope="all"` excludes ingested reference chunks and
    `scope="library"` returns only them, end to end through
    MemorySearchTool (not a fake duck-typed index)."""
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.embedding import FastembedProvider
    from durin.memory.reference import store_and_index_reference
    from durin.memory.storage import load_entry
    from durin.memory.store import store_memory
    from durin.memory.vector_index import VectorIndex

    with _stub_fastembed():
        vi = VectorIndex(tmp_path, FastembedProvider(_TEST_MODEL))
        stored = store_memory(tmp_path, content="alpha memory body", headline="alpha-memory")
        vi.upsert(load_entry(Path(stored["path"])), stored["class"], Path(stored["path"]))

        store_and_index_reference(
            tmp_path, "alpha-manual",
            "alpha protocol reference content", vector_index=vi,
        )

        search = MemorySearchTool(
            workspace=tmp_path,
            embedding_model=_TEST_MODEL,
        )
        default = await search.execute(query="alpha", scope="all", level="warm")
        library = await search.execute(query="alpha", scope="library", level="warm")

    default_uris = {r["uri"] for r in default["results"]}
    library_uris = {r["uri"] for r in library["results"]}
    assert not any(u.startswith("memory/reference/") for u in default_uris)
    assert any(u.startswith("memory/reference/") for u in library_uris)
