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


# ---------------------------------------------------------------------------
# index coverage: the grep leg reads only what the FTS index does not hold
# ---------------------------------------------------------------------------


def _bump_mtime(path: Path, seconds: float) -> None:
    import os
    st = path.stat()
    os.utime(path, (st.st_atime + seconds, st.st_mtime + seconds))


def test_coverage_skips_an_entry_the_index_holds_and_reads_it_again_once_edited(tmp_path: Path) -> None:
    from durin.memory.indexer import reindex_one_file
    from durin.memory.search import IndexCoverage
    from durin.memory.store import store_memory

    r = store_memory(tmp_path, content="la forja vieja de Mithral Hall", class_name="episodic")
    path = Path(r["path"])
    uri = f"memory/episodic/{r['id']}"

    cov = IndexCoverage.load(tmp_path)  # nothing indexed yet: read it
    assert [x.uri for x in search_memory(tmp_path, "forja vieja", coverage=cov)] == [uri]
    assert (cov.scanned, cov.skipped) == (1, 0)

    reindex_one_file(tmp_path, path)
    cov = IndexCoverage.load(tmp_path)  # indexed and unchanged: skip it
    assert search_memory(tmp_path, "forja vieja", coverage=cov) == []
    assert (cov.scanned, cov.skipped) == (0, 1)

    _bump_mtime(path, 5)
    cov = IndexCoverage.load(tmp_path)  # newer on disk than indexed: read it
    assert [x.uri for x in search_memory(tmp_path, "forja vieja", coverage=cov)] == [uri]
    assert (cov.scanned, cov.skipped) == (1, 0)

    # No coverage = the full walk, unchanged for callers that want it.
    assert [x.uri for x in search_memory(tmp_path, "forja vieja")] == [uri]


def test_coverage_applies_to_entity_pages_skills_references_and_sessions(tmp_path: Path) -> None:
    from durin.memory.entity_page import EntityPage
    from durin.memory.fts_index import FTSIndex
    from durin.memory.indexer import reindex_one_file
    from durin.memory.search import IndexCoverage

    page_path = tmp_path / "memory" / "entities" / "person" / "bruenor.md"
    EntityPage(type="person", name="Bruenor", aliases=[], body="lleva un hacha rúnica").save(page_path)
    ref_path = tmp_path / "memory" / "references" / "forja.md"
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text("# La forja\n\nun hacha rúnica se templa aquí\n", encoding="utf-8")
    session_path = tmp_path / "sessions" / "websocket_x.md"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text("## turn-1\n\nuser: hacha rúnica\n", encoding="utf-8")

    cov = IndexCoverage.load(tmp_path)
    found = {x.uri for x in search_memory(tmp_path, "hacha rúnica", coverage=cov)}
    assert {"memory/entity_page/person:bruenor", "sessions/websocket_x.md#turn-1"} <= found
    assert any(u.startswith("memory/reference") or u.startswith("reference:") for u in found)
    assert cov.scanned == 3

    reindex_one_file(tmp_path, page_path)
    reindex_one_file(tmp_path, ref_path)
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(uri="sessions/websocket_x.md#turn-1", path="sessions/websocket_x.md",
                   type_="session", entity_type=None, text="user: hacha rúnica",
                   mtime=session_path.stat().st_mtime)
    cov = IndexCoverage.load(tmp_path)
    assert search_memory(tmp_path, "hacha rúnica", coverage=cov) == []
    assert (cov.scanned, cov.skipped) == (0, 3)

    # A session that received a new turn is newer than its indexed turns.
    session_path.write_text(session_path.read_text() + "\n## turn-2\n\nuser: hacha rúnica otra vez\n", encoding="utf-8")
    _bump_mtime(session_path, 5)
    cov = IndexCoverage.load(tmp_path)
    assert {x.uri for x in search_memory(tmp_path, "hacha rúnica", coverage=cov)} == {
        "sessions/websocket_x.md#turn-1", "sessions/websocket_x.md#turn-2",
    }
    assert (cov.scanned, cov.skipped) == (1, 2)


def test_coverage_without_an_index_reads_everything(tmp_path: Path) -> None:
    from durin.memory.search import IndexCoverage
    from durin.memory.store import store_memory

    r = store_memory(tmp_path, content="yunque nuevo", class_name="episodic")
    cov = IndexCoverage.load(tmp_path)
    assert [x.uri for x in search_memory(tmp_path, "yunque", coverage=cov)] == [f"memory/episodic/{r['id']}"]
    assert cov.scanned == 1
