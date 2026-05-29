"""Tests for ``memory_drill_batch`` (audit H6, 2026-05-29).

After A+B (H5 markers + reworded drill description) bench v4 showed
drill ratio drop 42%, but the agent still drills 1.29× per search.
When drilling IS warranted (preview blocks), the agent typically
needs MULTIPLE bodies to answer (cross-reference between hits) and
fires one tool call per URI — N round-trips for N drills.

H6 ships ``memory_drill_batch(uris=[...])`` so the agent retrieves
N bodies in a single tool call. Same content, one round-trip,
lower latency.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _seed_entry(workspace: Path, body: str, headline: str) -> str:
    """Persist one entry and return its filename stem (== uri)."""
    from durin.memory.store import store_memory
    result = store_memory(workspace, content=body, headline=headline)
    return Path(result["path"]).stem


async def test_batch_returns_all_requested_bodies(tmp_path: Path) -> None:
    """Happy path: N URIs → N bodies in a single response."""
    from durin.agent.tools.memory_drill_batch import MemoryDrillBatchTool

    id_a = _seed_entry(tmp_path, body="alpha body content", headline="A")
    id_b = _seed_entry(tmp_path, body="beta body content", headline="B")
    id_c = _seed_entry(tmp_path, body="gamma body content", headline="C")

    tool = MemoryDrillBatchTool(workspace=tmp_path)
    result = await tool.execute(uris=[
        f"memory/episodic/{id_a}",
        f"memory/episodic/{id_b}",
        f"memory/episodic/{id_c}",
    ])

    assert "results" in result
    assert len(result["results"]) == 3
    bodies = [r.get("content", "") for r in result["results"]]
    assert any("alpha body content" in b for b in bodies)
    assert any("beta body content" in b for b in bodies)
    assert any("gamma body content" in b for b in bodies)


async def test_batch_preserves_uri_order(tmp_path: Path) -> None:
    """Results come back in the same order the agent requested so it
    can match content to its mental model of which uri was which."""
    from durin.agent.tools.memory_drill_batch import MemoryDrillBatchTool

    id_a = _seed_entry(tmp_path, body="first", headline="first")
    id_b = _seed_entry(tmp_path, body="second", headline="second")

    tool = MemoryDrillBatchTool(workspace=tmp_path)
    uris = [f"memory/episodic/{id_a}", f"memory/episodic/{id_b}"]
    result = await tool.execute(uris=uris)

    assert [r["uri"] for r in result["results"]] == uris


async def test_batch_individual_errors_dont_kill_the_batch(
    tmp_path: Path,
) -> None:
    """A bad uri (missing / malformed) reports an error in its own
    record; valid uris still come back populated."""
    from durin.agent.tools.memory_drill_batch import MemoryDrillBatchTool

    id_ok = _seed_entry(tmp_path, body="real body", headline="r")
    tool = MemoryDrillBatchTool(workspace=tmp_path)
    result = await tool.execute(uris=[
        f"memory/episodic/{id_ok}",
        "memory/episodic/nonexistent_id_999",
    ])

    assert len(result["results"]) == 2
    ok = result["results"][0]
    assert "real body" in ok.get("content", "")
    bad = result["results"][1]
    assert bad.get("error"), "missing uri must carry an error string"
    # Valid result must NOT carry an error.
    assert not ok.get("error")


async def test_batch_rejects_empty_list(tmp_path: Path) -> None:
    from durin.agent.tools.memory_drill_batch import MemoryDrillBatchTool
    tool = MemoryDrillBatchTool(workspace=tmp_path)
    result = await tool.execute(uris=[])
    assert "error" in result


async def test_batch_rejects_oversize_list(tmp_path: Path) -> None:
    """Cap protects context window — drilling 50 bodies in one call
    would dump tens of thousands of tokens at the agent."""
    from durin.agent.tools.memory_drill_batch import (
        MemoryDrillBatchTool, MAX_BATCH_URIS,
    )
    tool = MemoryDrillBatchTool(workspace=tmp_path)
    too_many = [f"memory/episodic/x{i}" for i in range(MAX_BATCH_URIS + 1)]
    result = await tool.execute(uris=too_many)
    assert "error" in result
    assert str(MAX_BATCH_URIS) in result["error"]


def test_batch_tool_description_mentions_when_to_use() -> None:
    """The description must steer the LLM toward batch when drilling
    >1 uri — without it the LLM would still fire one drill per uri."""
    from durin.agent.tools.memory_drill_batch import MemoryDrillBatchTool
    desc = MemoryDrillBatchTool(workspace=Path("/tmp")).description.lower()
    # Key concepts the description must convey.
    assert "preview" in desc, "must reference the `preview` marker"
    assert "single" in desc or "one call" in desc, (
        "must explain the round-trip benefit"
    )


def test_batch_tool_name_matches_convention() -> None:
    """The tool name must match the convention so the agent loop's
    tool registry picks it up via the standard registration path."""
    from durin.agent.tools.memory_drill_batch import MemoryDrillBatchTool
    assert MemoryDrillBatchTool(workspace=Path("/tmp")).name == (
        "memory_drill_batch"
    )
