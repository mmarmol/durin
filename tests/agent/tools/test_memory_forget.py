"""Tests for the memory_forget agent tool."""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

from durin.agent.tools.context import ToolContext
from durin.agent.tools.loader import ToolLoader
from durin.agent.tools.memory_forget import MemoryForgetTool
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import Config
from durin.memory.fts_index import FTSIndex
from durin.memory.indexer import reindex_one_file
from durin.memory.provenance import author_scope
from durin.memory.store import store_memory


def _store_stable(ws: Path) -> str:
    with author_scope("agent_created"):
        res = store_memory(
            ws, content="mxhero profile", class_name="stable",
            entities=["company:mxhero"],
            valid_from=datetime.date(2026, 6, 4),
        )
    return res["id"]


def test_forget_tool_archives_and_unindexes(tmp_path: Path) -> None:
    entry_id = _store_stable(tmp_path)
    entry_path = tmp_path / "memory" / "stable" / f"{entry_id}.md"
    uri = f"memory/stable/{entry_id}"
    reindex_one_file(tmp_path, entry_path, trigger="test")

    tool = MemoryForgetTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(uri=uri, reason="duplicate"))

    assert result["status"] == "forgotten"
    assert result["archived_to"] == f"memory/archive/stable/{entry_id}.md"
    assert not entry_path.exists()
    with FTSIndex.open(tmp_path) as idx:
        assert uri not in {u for u, _ in idx.known_uris()}


def test_forget_tool_missing_uri(tmp_path: Path) -> None:
    tool = MemoryForgetTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(uri="  "))
    assert "error" in result


def test_forget_tool_refuses_entities(tmp_path: Path) -> None:
    tool = MemoryForgetTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(uri="memory/entities/person:marcelo"))
    assert "error" in result


def test_forget_tool_nonexistent_entry(tmp_path: Path) -> None:
    tool = MemoryForgetTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(uri="memory/stable/nope"))
    assert "error" in result


def test_forget_tool_emits_telemetry(tmp_path: Path) -> None:
    """A successful forget fires a memory.forget telemetry event."""
    import json

    from durin.telemetry.logger import (
        bind_telemetry,
        get_session_logger,
        reset_telemetry,
    )

    entry_id = _store_stable(tmp_path)
    uri = f"memory/stable/{entry_id}"
    tool = MemoryForgetTool(workspace=tmp_path)

    logger = get_session_logger("websocket:c1", base_dir=tmp_path)
    token = bind_telemetry(logger)
    try:
        asyncio.run(tool.execute(uri=uri, reason="duplicate"))
    finally:
        reset_telemetry(token)

    events = [
        json.loads(line)
        for line in logger.path.read_text(encoding="utf-8").splitlines()
    ]
    forget = [e for e in events if e["type"] == "memory.forget"]
    assert len(forget) == 1
    assert forget[0]["data"]["uri"] == uri
    assert forget[0]["data"]["class_name"] == "stable"
    assert forget[0]["data"]["reason"] == "duplicate"


def test_forget_tool_registered_in_core_scope(tmp_path: Path) -> None:
    """Auto-discovered + registered for the foreground agent (core scope)."""
    loader = ToolLoader()
    registry = ToolRegistry()
    ctx = ToolContext(config=Config().tools, workspace=str(tmp_path))
    loader.load(ctx, registry, scope="core")
    assert "memory_forget" in set(registry.tool_names)
