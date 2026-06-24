from pathlib import Path
from datetime import datetime, timezone
import asyncio
import json
from durin.agent.tools.memory_lineage_tools import (
    MemoryReadEntityTool,
    MemoryEntityLineageTool,
    MemorySourceSessionTool,
)
from durin.memory.memory_writer import write_entity
from durin.memory.field_patch import FieldPatch
from durin.agent.tools.loader import ToolLoader
from durin.agent.tools.registry import ToolRegistry
from durin.agent.tools.context import ToolContext
from durin.config.schema import Config

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)

def test_read_entity_returns_full_markdown(tmp_path):
    write_entity(tmp_path, "place:torrent", [FieldPatch(kind="attribute",
        key="region", value="Valencia", author="dream", source_ref="s", at=NOW)],
        create=True, name="Torrent")
    out = asyncio.run(MemoryReadEntityTool(tmp_path).execute(ref="place:torrent"))
    assert "Torrent" in out["markdown"]
    assert "region" in out["markdown"] and "Valencia" in out["markdown"]

def test_read_entity_missing(tmp_path):
    out = asyncio.run(MemoryReadEntityTool(tmp_path).execute(ref="place:nope"))
    assert "error" in out


def test_entity_lineage_lists_commits(tmp_path):
    write_entity(tmp_path, "place:torrent", [FieldPatch(kind="attribute",
        key="region", value="Valencia", author="dream", source_ref="s", at=NOW)],
        create=True, name="Torrent")
    out = asyncio.run(MemoryEntityLineageTool(tmp_path).execute(ref="place:torrent"))
    assert out["commits"], out
    assert "when" in out["commits"][0] and "message" in out["commits"][0]


def test_source_session_resolves_turns(tmp_path):
    sdir = tmp_path / "sessions"; sdir.mkdir(parents=True)
    (sdir / "s1.jsonl").write_text(
        json.dumps({"_type": "metadata", "key": "s1"}) + "\n" +
        json.dumps({"role": "user", "content": "Torrent es un municipio de Valencia"}) + "\n",
        encoding="utf-8")
    write_entity(tmp_path, "place:torrent", [FieldPatch(kind="attribute",
        key="region", value="Valencia", author="dream",
        source_ref="[[sessions/s1.md#turn-1]]", at=NOW)],
        create=True, name="Torrent")
    out = asyncio.run(MemorySourceSessionTool(tmp_path).execute(ref="place:torrent"))
    assert any("municipio de Valencia" in s.get("content", "") for s in out["sources"])


def test_lineage_tools_auto_discovered(tmp_path):
    loader = ToolLoader()
    registry = ToolRegistry()
    ctx = ToolContext(config=Config().tools, workspace=str(tmp_path))
    loader.load(ctx, registry, scope="core")
    names = set(registry.tool_names)
    assert {"memory_read_entity", "memory_entity_lineage", "memory_source_session"} <= names
