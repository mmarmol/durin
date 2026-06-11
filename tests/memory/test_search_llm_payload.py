"""Tests for the P4b memory_search payload split (2026-06-10).

The LLM only reads ``sectioned_rendered`` — shipping the raw
``results`` dicts to the model pays for every hit twice. The agent
tool path (``create()``) drops them; programmatic callers (graph_api /
webui, bench scripts, direct constructions) keep the structured array.
"""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path
from unittest.mock import MagicMock

from durin.agent.tools.memory_search import MemorySearchTool
from durin.memory.store import store_memory


def _store(ws: Path, content: str) -> None:
    store_memory(
        ws, content=content, entities=["person:marcelo"],
        valid_from=datetime.date(2026, 6, 1),
    )


def _search(tool: MemorySearchTool, query: str, **kwargs) -> dict:
    return asyncio.run(tool.execute(query=query, **kwargs))


def test_direct_construction_keeps_raw_results(tmp_path: Path) -> None:
    _store(tmp_path, "marcelo prefers pytest over unittest")
    tool = MemorySearchTool(workspace=tmp_path)

    payload = _search(tool, "pytest")

    assert payload["total"] >= 1
    assert "results" in payload
    assert payload["results"][0]["uri"]
    assert payload["sectioned_rendered"]


def test_llm_path_drops_raw_results_but_keeps_rendered(tmp_path: Path) -> None:
    _store(tmp_path, "marcelo prefers pytest over unittest")
    tool = MemorySearchTool(workspace=tmp_path, include_raw_results=False)

    payload = _search(tool, "pytest")

    assert payload["total"] >= 1
    assert "results" not in payload
    assert "pytest" in payload["sectioned_rendered"]
    assert payload["strategy"]


def test_archive_path_honours_flag(tmp_path: Path) -> None:
    tool = MemorySearchTool(workspace=tmp_path, include_raw_results=False)
    payload = _search(tool, "anything", scope="archive")
    assert "results" not in payload
    assert payload["total"] == 0

    tool_raw = MemorySearchTool(workspace=tmp_path)
    payload_raw = _search(tool_raw, "anything", scope="archive")
    assert payload_raw["results"] == []


def test_create_drops_raw_results_for_agent_path(tmp_path: Path) -> None:
    ctx = MagicMock()
    ctx.workspace = str(tmp_path)
    ctx.app_config = None
    ctx.scope = "core"

    tool = MemorySearchTool.create(ctx)

    assert tool._include_raw_results is False
