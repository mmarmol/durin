"""E2E tests for T1 wiring (doc 24 §3): full path, not units.

These tests exercise the wired-up integrations from doc 24:

- E2E-1: ``memory_search`` invokes entity-aware ranker on real LanceDB
  rows when the query mentions a known entity (W1+W2).
- E2E-2: ``durin memory dream`` upserts the consolidated entity page
  into the vector index, so the page is retrievable via
  ``VectorIndex.search`` (W3).
- E2E-3: Fresh ``MemorySearchTool`` rebuilds the AliasIndex lazily on
  first call (no persistent sidecar) and applies entity-aware ranking.
- E2E-5: ``durin memory absorb`` CLI merges two pages, archives one,
  drops the absorbed ref from alias_index and vector_index (W4).

Uses the same stubbed fastembed pattern as ``test_phase2_smoke.py`` so
the tests stay hermetic and fast. The point is wiring correctness, not
retrieval quality — quality is benchmarked separately (LoCoMo etc).
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.memory.vector_index import vector_index_available

pytestmark = pytest.mark.skipif(
    not vector_index_available(),
    reason="lancedb not installed; install durin[memory] to run these tests",
)


_TEST_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

_STUB_CATALOG = [{"model": _TEST_MODEL, "dim": 8, "size_in_GB": 0.22}]


class _FakeTextEmbedding:
    """Deterministic stub: first-char + length seeded vectors.

    Sufficient to exercise the LanceDB + ranker path without pulling
    real models. RRF rank fusion in the entity ranker does NOT depend
    on cosine distance for the entity-match component — it uses rank
    derived from class_name + entities — so the entity-aware reranking
    is testable with arbitrary embeddings.
    """

    @staticmethod
    def list_supported_models():
        return list(_STUB_CATALOG)

    def __init__(self, model_name=None, **_):
        self.model_name = model_name

    def embed(self, texts):
        for text in texts:
            first = float(ord(text[0])) if text else 0.0
            length = float(len(text))
            yield [first, length] + [0.0] * 6


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
# E2E-1: memory_search applies entity-aware ranking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e1_memory_search_invokes_entity_aware_ranker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Query mentioning a known entity → telemetry shows entity_aware ranking.

    Setup: 1 entity page + 3 tagged entries + 2 noise entries, all indexed.
    Action: query "what does Marcelo prefer".
    Assert: ranking=="entity_aware", query_entities_count>=1, results non-empty.
    """
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.embedding import FastembedProvider
    from durin.memory.entity_page import EntityPage
    from durin.memory.store import store_memory
    from durin.memory.vector_index import VectorIndex

    with _stub_fastembed():
        # 1 entity page on disk + index
        page = EntityPage(
            type="person",
            name="Marcelo Marmol",
            aliases=["Marcelo", "marcelo"],
            body="## Current State\nPrefers pytest.\n",
            dream_processed_through="2026-04-15",
        )
        page_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
        page.save(page_path)

        provider = FastembedProvider(_TEST_MODEL)
        vi = VectorIndex(tmp_path, provider)
        vi.upsert_entity_page(
            entity_ref="person:marcelo",
            name=page.name,
            aliases=page.aliases,
            body=page.body,
            path=page_path,
        )

        # 3 entries tagged with the entity
        for i in range(3):
            store_memory(
                tmp_path,
                content=f"marcelo observation {i}",
                entities=["person:marcelo"],
            )
        # 2 noise entries
        for i in range(2):
            store_memory(
                tmp_path,
                content=f"unrelated topic {i}",
                entities=[],
            )
        # Reindex everything so memory entries land in the same table.
        vi.rebuild_from_workspace()
        # Re-upsert the entity page (rebuild_from_workspace only walks
        # memory/<class>/*, not entity pages).
        vi.upsert_entity_page(
            entity_ref="person:marcelo",
            name=page.name,
            aliases=page.aliases,
            body=page.body,
            path=page_path,
        )

        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "durin.agent.tools.memory_search.emit_tool_event",
            lambda t, d: events.append((t, d)),
        )

        tool = MemorySearchTool(workspace=tmp_path, embedding_model=_TEST_MODEL)
        out = await tool.execute(
            query="what does Marcelo prefer", scope="dreamed", level="warm",
        )

    assert out["total"] > 0
    # W1 wiring: ranking flag flipped to entity_aware when query matches.
    assert out["ranking"] == "entity_aware"

    vector_events = [e for e in events if e[0] == "memory.recall.vector"]
    assert len(vector_events) == 1
    payload = vector_events[0][1]
    assert payload["ranking"] == "entity_aware"
    assert payload["query_entities_count"] >= 1


# ---------------------------------------------------------------------------
# E2E-2: cmd_dream upserts entity page into vector index (W3)
# ---------------------------------------------------------------------------


def _stub_consolidation_response(entity_ref: str) -> str:
    """Well-formed dream LLM response for an entity (page + commit blocks)."""
    type_, slug = entity_ref.split(":", 1)
    return (
        "===PAGE===\n"
        "---\n"
        f"type: {type_}\n"
        f"name: {slug.title()}\n"
        f"aliases: [{slug}, {slug.title()}]\n"
        "dream_processed_through: 2026-05-20\n"
        "---\n"
        "\n"
        f"# {slug.title()}\n"
        "\n"
        "## Current State\n"
        "First consolidation pass.\n"
        "===COMMIT===\n"
        f"Consolidate {entity_ref} (rev 1)\n"
        "\n"
        "Initial pass.\n"
        "\n"
        f"Sources: e1, e2, e3\n"
        f"Entities-touched: {entity_ref}\n"
        "Cursor-after: 2026-05-20\n"
        "===END===\n"
    )


def test_e2e2_cmd_dream_upserts_page_into_vector_index(
    tmp_path: Path,
) -> None:
    """After cmd_dream completes, VectorIndex.search() returns the page row.

    This is the direct W3 validation: without the fix, the page lands on
    disk but never enters LanceDB, and memory_search can't surface it.
    """
    from durin.cli.memory_cmd import memory_app
    from durin.memory.embedding import FastembedProvider
    from durin.memory.store import store_memory
    from durin.memory.vector_index import VectorIndex

    runner = CliRunner()

    with _stub_fastembed():
        for i in range(3):
            store_memory(
                tmp_path,
                content=f"observation about marcelo #{i}",
                entities=["person:marcelo"],
            )

        # Patch: workspace, load_config (so memory.enabled is true), and
        # the LLM invoker the DreamConsolidator dispatches to.
        with patch(
            "durin.cli.memory_cmd._workspace_root",
            return_value=tmp_path,
        ), patch(
            "durin.memory.dream.default_llm_invoke",
            return_value=_stub_consolidation_response("person:marcelo"),
        ), patch(
            "durin.cli.memory_cmd.load_config",
        ) as mock_load_config:
            cfg = mock_load_config.return_value
            cfg.memory.enabled = True
            cfg.memory.embedding.model = _TEST_MODEL
            cfg.workspace_path.return_value = tmp_path

            result = runner.invoke(memory_app, ["dream"])

        assert result.exit_code == 0, result.output
        page_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
        assert page_path.exists()

        # The actual W3 assertion: the page made it into the vector index.
        vi = VectorIndex(tmp_path, FastembedProvider(_TEST_MODEL))
        rows = vi.search("Marcelo", top_k=10)
        entity_rows = [
            r for r in rows
            if r.get("class_name") == "entity_page"
            and r.get("id") == "person:marcelo"
        ]
        assert entity_rows, (
            f"entity_page row not in vector index after dream; "
            f"got class_names={[r.get('class_name') for r in rows]}"
        )


# ---------------------------------------------------------------------------
# E2E-3: Cold-start alias_index rebuild on first memory_search call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e3_memory_search_rebuilds_alias_index_lazily(
    tmp_path: Path,
) -> None:
    """First call to memory_search with no .aliases.json sidecar.

    Setup: 2 entity pages on disk. Process-wide alias cache cleared
    (doc 25 §2.C). Tool is a fresh instance (simulates cold start).
    Action: query that mentions a known alias.
    Assert: shared cache builds on first execute, ranker activates.
    """
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.aliases_cache import _cache_size, _clear_all
    from durin.memory.embedding import FastembedProvider
    from durin.memory.entity_page import EntityPage
    from durin.memory.vector_index import VectorIndex

    _clear_all()
    with _stub_fastembed():
        for slug, alias in [("marcelo", "Marcelo"), ("durin", "Durin")]:
            type_ = "person" if slug == "marcelo" else "project"
            page = EntityPage(
                type=type_,
                name=alias,
                aliases=[alias, slug],
                body="## Current State\nSomething.\n",
            )
            page_path = (
                tmp_path / "memory" / "entities" / type_ / f"{slug}.md"
            )
            page.save(page_path)

            vi = VectorIndex(tmp_path, FastembedProvider(_TEST_MODEL))
            vi.upsert_entity_page(
                entity_ref=f"{type_}:{slug}",
                name=page.name,
                aliases=page.aliases,
                body=page.body,
                path=page_path,
            )

        # Confirm cold start: no persisted alias index sidecar exists
        # and the process-wide shared cache holds nothing yet.
        assert not (tmp_path / "memory" / ".aliases.json").exists()
        assert _cache_size() == 0

        tool = MemorySearchTool(workspace=tmp_path, embedding_model=_TEST_MODEL)
        out = await tool.execute(
            query="ask Marcelo about the design", scope="dreamed",
        )

    # The shared cache built the workspace's index lazily on first execute.
    assert _cache_size() == 1
    # Query matched the alias, ranker activated.
    assert out["ranking"] == "entity_aware"
    _clear_all()


# ---------------------------------------------------------------------------
# E2E-5: durin memory absorb merges, archives, deindexes
# ---------------------------------------------------------------------------


def test_e2e5_absorb_full_pipeline(tmp_path: Path) -> None:
    """End-to-end: absorb merges aliases, archives, deindexes from vector.

    Verifies W4(c): CLI command runs the full EntityAbsorption pipeline
    including the vector_index.delete_by_id() call.
    """
    from durin.cli.memory_cmd import memory_app
    from durin.memory.embedding import FastembedProvider
    from durin.memory.entity_page import EntityPage
    from durin.memory.vector_index import VectorIndex

    runner = CliRunner()

    with _stub_fastembed():
        for slug, alias in [("marcelo", "Marcelo"), ("marcelo_m", "Marcelo")]:
            page = EntityPage(
                type="person",
                name=alias,
                aliases=[alias, slug.replace("_", " ")],
            )
            page_path = (
                tmp_path / "memory" / "entities" / "person" / f"{slug}.md"
            )
            page.save(page_path)
            vi = VectorIndex(tmp_path, FastembedProvider(_TEST_MODEL))
            vi.upsert_entity_page(
                entity_ref=f"person:{slug}",
                name=page.name,
                aliases=page.aliases,
                body=page.body,
                path=page_path,
            )

        # Confirm both rows are in the index before absorb.
        rows_before = vi.search("Marcelo", top_k=10)
        ids_before = {r["id"] for r in rows_before}
        assert "person:marcelo" in ids_before
        assert "person:marcelo_m" in ids_before

        # Patch _build_vector_index_optional so the CLI uses a real index
        # pointed at this tmp_path (default would respect ~/.durin/config).
        def _build_vi():
            return VectorIndex(tmp_path, FastembedProvider(_TEST_MODEL))

        with patch(
            "durin.cli.memory_cmd._workspace_root",
            return_value=tmp_path,
        ), patch(
            "durin.cli.memory_cmd._build_vector_index_optional",
            side_effect=_build_vi,
        ):
            result = runner.invoke(
                memory_app,
                ["absorb", "person:marcelo", "person:marcelo_m",
                 "--reason", "same person", "--yes"],
            )

        assert result.exit_code == 0, result.output

        # Canonical merged.
        canonical_path = (
            tmp_path / "memory" / "entities" / "person" / "marcelo.md"
        )
        merged = EntityPage.from_file(canonical_path)
        assert merged is not None
        assert any(a.lower() == "marcelo m" for a in merged.aliases)

        # Absorbed archived. Phase 0 deliverable 5: top-level archive.
        absorbed_orig = (
            tmp_path / "memory" / "entities" / "person" / "marcelo_m.md"
        )
        assert not absorbed_orig.exists()
        archived = (
            tmp_path / "memory" / "archive" / "entities" / "person"
            / "marcelo_m.md"
        )
        assert archived.exists()

        # Vector index: absorbed row gone.
        rows_after = vi.search("Marcelo", top_k=10)
        ids_after = {r["id"] for r in rows_after}
        assert "person:marcelo_m" not in ids_after
        assert "person:marcelo" in ids_after

        # Git commit exists.
        from durin.utils.git_repo import GitRepo

        repo = GitRepo(
            tmp_path / "memory",
            default_author="durin-dream",
            default_email="dream@durin.local",
        )
        commits = repo.log(max_count=5)
        assert any("Absorb person:marcelo_m" in c.subject for c in commits)
