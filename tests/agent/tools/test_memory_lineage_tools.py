from pathlib import Path
from datetime import datetime, timezone
import asyncio
from durin.agent.tools.memory_lineage_tools import MemoryReadEntityTool
from durin.memory.memory_writer import write_entity
from durin.memory.field_patch import FieldPatch

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
