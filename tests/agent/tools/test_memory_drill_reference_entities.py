import asyncio
from datetime import datetime, timezone
from pathlib import Path

from durin.agent.tools.memory_drill import MemoryDrillTool
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.reference import ingest_reference


def test_drilling_a_reference_lists_its_entities(tmp_path: Path) -> None:
    ingest_reference(tmp_path, "Thinking Fast and Slow", "# T\n\nbody.\n")
    write_entity(tmp_path, "topic:two-systems",
                 [FieldPatch(kind="derived_from", value="reference:thinking-fast-and-slow",
                             author="dream", source_ref="s", at=datetime.now(timezone.utc))],
                 create=True, name="Two systems")

    out = asyncio.run(MemoryDrillTool(workspace=tmp_path).execute(uri="reference:thinking-fast-and-slow"))

    assert "body." in out["content"]
    assert out["content"].rstrip().endswith("Entities distilled from this document: topic:two-systems")
