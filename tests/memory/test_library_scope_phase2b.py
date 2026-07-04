"""Phase 2b: the Library recall-scope flip (contamination isolation).

Ingested reference material is kept out of the default recall pool and is
reachable only via an explicit ``scope="library"`` search. These tests cover
the pipeline-level filter (with a fake vector index, no embedding model) and
the memory_search tool end-to-end via the FTS/grep path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.indexer import reindex_one_file
from durin.memory.reference import ingest_reference
from durin.memory.search_pipeline import run_search_pipeline


class _FakeVectorIndex:
    """Emits one ordinary memory row and one reference chunk row in the
    production ``id``/``class_name`` shape the pipeline normalises."""

    def search(self, query, top_k=50):  # noqa: ARG002
        return [
            {
                "id": "9b6f1c81724a",
                "class_name": "episodic",
                "path": "memory/episodic/9b6f1c81724a.md",
                "summary": "an ordinary memory note",
            },
            {
                "id": "reference:handbook#0",
                "class_name": "reference",
                "path": "memory/references/handbook.md",
                "summary": "a chunk of an ingested book",
            },
        ]


def _uris(result) -> set[str]:
    return {h.uri for h in result.hits}


def test_pipeline_exclude_drops_reference(tmp_path: Path) -> None:
    res = run_search_pipeline(
        tmp_path, "note", vector_index=_FakeVectorIndex(),
        library_mode="exclude",
    )
    uris = _uris(res)
    assert "memory/episodic/9b6f1c81724a" in uris
    assert not any(u.startswith("memory/reference/") for u in uris)


def test_pipeline_only_keeps_reference(tmp_path: Path) -> None:
    res = run_search_pipeline(
        tmp_path, "note", vector_index=_FakeVectorIndex(),
        library_mode="only",
    )
    uris = _uris(res)
    assert any(u.startswith("memory/reference/") for u in uris)
    assert "memory/episodic/9b6f1c81724a" not in uris


def test_pipeline_none_is_backward_compatible(tmp_path: Path) -> None:
    res = run_search_pipeline(
        tmp_path, "note", vector_index=_FakeVectorIndex(),
    )
    uris = _uris(res)
    assert "memory/episodic/9b6f1c81724a" in uris
    assert any(u.startswith("memory/reference/") for u in uris)


@pytest.mark.asyncio
async def test_tool_contamination_via_fts(tmp_path: Path) -> None:
    """End-to-end through memory_search (FTS/grep path, no embedding model):
    a default-scope query must not surface the ingested document; a
    library-scope query must."""
    ws = tmp_path / "ws"
    ws.mkdir()
    md = "# Manual\n\nThe zorptastic protocol governs the whole system.\n"
    res = ingest_reference(ws, "zorpmanual", md)
    slug = res.ref.split(":", 1)[1]
    reindex_one_file(ws, ws / "memory" / "references" / f"{slug}.md")

    from durin.agent.tools.memory_search import MemorySearchTool

    tool = MemorySearchTool(workspace=ws)  # no embedding model → FTS + grep

    default = await tool.execute(query="zorptastic", scope="all")
    library = await tool.execute(query="zorptastic", scope="library")

    assert "zorptastic" not in default["sectioned_rendered"]
    assert "zorpmanual" not in default["sectioned_rendered"]
    assert (
        "zorptastic" in library["sectioned_rendered"]
        or "zorpmanual" in library["sectioned_rendered"]
    )
