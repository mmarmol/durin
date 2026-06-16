"""Tests for memory_search (durin.memory.search + MemorySearchTool)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from durin.memory.search import search_memory
from durin.memory.store import store_memory


def _write_session_view(
    workspace: Path,
    key: str,
    *,
    body_md: str,
    tags: dict | None = None,
) -> None:
    sessions = workspace / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{key}.md").write_text(body_md, encoding="utf-8")
    if tags is not None:
        meta = {"key": key, "version": 1, "derived": {"tags": tags}}
        (sessions / f"{key}.meta.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )


def _write_ingested(
    workspace: Path,
    entry_id: str,
    *,
    source_text: str,
    derived: dict | None = None,
) -> None:
    entry_dir = workspace / "ingested" / entry_id
    entry_dir.mkdir(parents=True, exist_ok=True)
    (entry_dir / "source.md").write_text(source_text, encoding="utf-8")
    meta = {"id": entry_id, "derived": derived or {}}
    (entry_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


# ---------------------------------------------------------------------------
# dreamed scope (memory/<class>/*.md)
# ---------------------------------------------------------------------------


def test_search_dreamed_matches_headline(tmp_path: Path) -> None:
    store_memory(tmp_path, content="Body about cache layer", headline="cache discussion")
    results = search_memory(tmp_path, "cache", scope="dreamed")
    assert len(results) == 1
    assert results[0].source == "memory"
    assert "cache" in results[0].headline.lower()


def test_search_dreamed_matches_body(tmp_path: Path) -> None:
    store_memory(tmp_path, content="we decided to drop pytest in favor of nose")
    results = search_memory(tmp_path, "pytest", scope="dreamed")
    assert len(results) == 1


def test_search_dreamed_matches_entity(tmp_path: Path) -> None:
    store_memory(tmp_path, content="body", entities=["person:marcelo", "project:durin"])
    results = search_memory(tmp_path, "project:durin", scope="dreamed")
    assert len(results) == 1


def test_search_dreamed_warm_returns_summary_not_body(tmp_path: Path) -> None:
    store_memory(
        tmp_path,
        content="full body here",
        summary="warm-tier summary",
    )
    results = search_memory(tmp_path, "body", scope="dreamed", level="warm")
    assert len(results) == 1
    assert results[0].summary == "warm-tier summary"
    assert results[0].body == ""


def test_search_dreamed_cold_returns_body(tmp_path: Path) -> None:
    store_memory(tmp_path, content="full body here", summary="summary")
    results = search_memory(tmp_path, "body", scope="dreamed", level="cold")
    assert len(results) == 1
    assert results[0].body == "full body here"


def test_search_dreamed_case_insensitive(tmp_path: Path) -> None:
    store_memory(tmp_path, content="The Cache Layer Discussion")
    results = search_memory(tmp_path, "cache", scope="dreamed")
    assert len(results) == 1


def test_search_dreamed_empty_workspace(tmp_path: Path) -> None:
    results = search_memory(tmp_path, "anything", scope="dreamed")
    assert results == []


# ---------------------------------------------------------------------------
# undreamed scope: sessions
# ---------------------------------------------------------------------------


def test_search_undreamed_matches_session_body_with_turn_anchor(tmp_path: Path) -> None:
    body = (
        "# Session abc\n"
        "\n"
        "## turn-1\n"
        "**user**\n"
        "hola que tal\n"
        "\n"
        "## turn-2\n"
        "**assistant**\n"
        "discutimos el cache layer\n"
    )
    _write_session_view(tmp_path, "abc", body_md=body)
    results = search_memory(tmp_path, "cache", scope="undreamed")
    assert len(results) == 1
    assert results[0].source == "sessions"
    assert results[0].uri == "sessions/abc.md#turn-2"


def test_search_undreamed_matches_session_tag(tmp_path: Path) -> None:
    body = "# Session\n\n## turn-1\nirrelevant body\n"
    _write_session_view(
        tmp_path,
        "abc",
        body_md=body,
        tags={"entities": ["durin"], "topics": ["memory-system"]},
    )
    results = search_memory(tmp_path, "memory-system", scope="undreamed")
    assert len(results) >= 1
    assert any(r.source == "sessions" for r in results)


def test_search_undreamed_handles_no_anchor_yet(tmp_path: Path) -> None:
    """Match in the file header before any ## turn-N still returns a result."""
    body = "# Session abc with a marcelo header mention\n\n## turn-1\nirrelevant\n"
    _write_session_view(tmp_path, "abc", body_md=body)
    results = search_memory(tmp_path, "marcelo", scope="undreamed")
    assert len(results) >= 1


# ---------------------------------------------------------------------------
# undreamed scope: ingested
# ---------------------------------------------------------------------------


def test_search_undreamed_matches_ingested_source(tmp_path: Path) -> None:
    _write_ingested(tmp_path, "doc-1", source_text="ingested body about cache")
    results = search_memory(tmp_path, "cache", scope="undreamed")
    assert len(results) == 1
    assert results[0].source == "ingested"
    assert results[0].uri == "ingested/doc-1/source"


def test_search_undreamed_matches_ingested_derived_summary(tmp_path: Path) -> None:
    _write_ingested(
        tmp_path,
        "doc-1",
        source_text="unrelated body",
        derived={"summary": "this doc is about caching strategies"},
    )
    results = search_memory(tmp_path, "caching", scope="undreamed")
    assert len(results) == 1


def test_search_undreamed_matches_ingested_entity(tmp_path: Path) -> None:
    _write_ingested(
        tmp_path,
        "doc-1",
        source_text="x",
        derived={"entities": ["marcelo"]},
    )
    results = search_memory(tmp_path, "marcelo", scope="undreamed")
    assert len(results) == 1


# ---------------------------------------------------------------------------
# all scope and edge cases
# ---------------------------------------------------------------------------


def test_search_all_combines_dreamed_and_undreamed(tmp_path: Path) -> None:
    store_memory(tmp_path, content="cache discussion in memory entry")
    _write_session_view(
        tmp_path,
        "abc",
        body_md="# s\n\n## turn-1\nmention of cache in session\n",
    )
    results = search_memory(tmp_path, "cache", scope="all")
    sources = {r.source for r in results}
    assert "memory" in sources
    assert "sessions" in sources


def test_search_empty_query_returns_empty(tmp_path: Path) -> None:
    store_memory(tmp_path, content="cache content")
    assert search_memory(tmp_path, "") == []
    assert search_memory(tmp_path, "   ") == []


def test_search_no_match(tmp_path: Path) -> None:
    store_memory(tmp_path, content="cache content")
    assert search_memory(tmp_path, "nonexistent") == []


# ---------------------------------------------------------------------------
# MemorySearchTool wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_returns_results_dict(tmp_path: Path) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool

    store_memory(tmp_path, content="cache layer learning")
    tool = MemorySearchTool(workspace=tmp_path)
    out = await tool.execute(query="cache")
    assert "results" in out
    assert out["total"] == 1
    assert out["results"][0]["source"] == "memory"


@pytest.mark.asyncio
async def test_tool_empty_query_error(tmp_path: Path) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool

    tool = MemorySearchTool(workspace=tmp_path)
    out = await tool.execute(query="")
    assert out == {"error": "query is required"}


@pytest.mark.asyncio
async def test_tool_invalid_scope_error(tmp_path: Path) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool

    tool = MemorySearchTool(workspace=tmp_path)
    out = await tool.execute(query="x", scope="bogus")
    assert "error" in out
    assert "scope" in out["error"]
