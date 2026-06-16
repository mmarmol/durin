"""Behaviour tests for the `limit` parameter on `MemorySearchTool` (A3).

Per `feedback_sync_tests_exercise_behavior` in personal memory: a sync
test that only compares the description string against the doc would
pass even if the code never honored the parameter. These tests
exercise the **behaviour** — they actually invoke the tool with
different `limit` values and assert the result count.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from durin.agent.tools.memory_search import (
    _PARAMETERS,
    MemorySearchTool,
)
from durin.memory.indexer import rebuild_fts_index
from durin.memory.store import store_memory


def _seed_n_entries(workspace: Path, n: int) -> None:
    """Write n episodic entries that all share a common keyword so they
    all match the same query."""
    for i in range(n):
        store_memory(
            workspace,
            content=f"common_keyword entry number {i}",
            class_name="episodic",
            headline=f"entry {i}",
        )
    rebuild_fts_index(workspace)


def test_limit_param_is_declared_in_schema() -> None:
    """First-line check: the canonical doc-04 promise of a `limit`
    param is reflected in the schema the LLM actually sees.

    `_PARAMETERS` is already a dict (output of `to_json_schema()`),
    not a Schema object — see `tool_parameters_schema` in
    `durin/agent/tools/schema.py`."""
    assert "limit" in _PARAMETERS["properties"]
    limit_schema = _PARAMETERS["properties"]["limit"]
    assert limit_schema["type"] == "integer"
    assert limit_schema.get("minimum") == 1
    assert limit_schema.get("maximum") == 50
    # `limit` is OPTIONAL — only `query` is required.
    assert "limit" not in _PARAMETERS.get("required", [])


def test_limit_default_is_10_when_omitted(tmp_path: Path) -> None:
    """Default behaviour preserved: when the agent omits `limit`,
    the tool returns up to 10 hits — matching the prior hard-coded
    behaviour, so no breaking change for existing callers."""
    _seed_n_entries(tmp_path, 15)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="common_keyword"))
    assert out["total"] <= 10
    # And we know 15 entries match, so the cap actually bit.
    assert out["total"] == 10


def test_limit_5_returns_at_most_5(tmp_path: Path) -> None:
    """The agent can shrink the response for chat-style short answers."""
    _seed_n_entries(tmp_path, 15)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="common_keyword", limit=5))
    assert out["total"] <= 5
    assert out["total"] == 5  # 15 matches, cap at 5


def test_limit_30_allows_more_results(tmp_path: Path) -> None:
    """The agent can widen the response for audit / investigative
    queries."""
    _seed_n_entries(tmp_path, 25)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="common_keyword", limit=30))
    # All 25 match; 30 lets them all through.
    assert out["total"] == 25


def test_limit_clamped_to_50_at_upper_bound(tmp_path: Path) -> None:
    """`limit=999` does NOT blow up the response — defensive clamp
    in `execute()` caps at 50 even if the schema validator is somehow
    bypassed."""
    _seed_n_entries(tmp_path, 60)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="common_keyword", limit=999))
    assert out["total"] <= 50


def test_limit_clamped_to_1_at_lower_bound(tmp_path: Path) -> None:
    """`limit=0` (or negative) does NOT return an empty response —
    defensive clamp raises to 1."""
    _seed_n_entries(tmp_path, 15)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="common_keyword", limit=0))
    assert out["total"] == 1


def test_limit_handles_non_integer_input_gracefully(
    tmp_path: Path,
) -> None:
    """If the LLM emits a string-shaped int, the tool coerces; if
    the value is non-coercible, the default 10 is used."""
    _seed_n_entries(tmp_path, 15)
    tool = MemorySearchTool(workspace=tmp_path)

    # "5" → coerced to 5
    out = asyncio.run(tool.execute(query="common_keyword", limit="5"))
    assert out["total"] == 5

    # garbage → falls back to default 10
    out = asyncio.run(tool.execute(query="common_keyword", limit="abc"))
    assert out["total"] == 10
