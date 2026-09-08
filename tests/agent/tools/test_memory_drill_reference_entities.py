import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from durin.agent.tools.memory_drill import MemoryDrillTool
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.reference import ingest_reference

_HEADER = "Entities distilled from this document: topic:two-systems"


def _prepare(tmp_path: Path, body: str = "# T\n\nbody.\n") -> None:
    ingest_reference(tmp_path, "Thinking Fast and Slow", body)
    write_entity(tmp_path, "topic:two-systems",
                 [FieldPatch(kind="derived_from", value="reference:thinking-fast-and-slow",
                             author="dream", source_ref="s", at=datetime.now(timezone.utc))],
                 create=True, name="Two systems")


@pytest.mark.parametrize("uri", [
    "reference:thinking-fast-and-slow",
    "reference:thinking-fast-and-slow#T",
    "memory/reference/thinking-fast-and-slow",
    "memory/references/thinking-fast-and-slow.md",
])
def test_drilling_a_reference_lists_its_entities(tmp_path: Path, uri: str) -> None:
    _prepare(tmp_path)

    out = asyncio.run(MemoryDrillTool(workspace=tmp_path).execute(uri=uri))

    assert out["content"].startswith(_HEADER + "\n\n")
    assert "body." in out["content"]


def test_batch_drill_lists_entities_on_the_reference_entry(tmp_path: Path) -> None:
    _prepare(tmp_path)

    out = asyncio.run(MemoryDrillTool(workspace=tmp_path).execute(
        uris=["memory/reference/thinking-fast-and-slow"],
    ))

    entry = out["results"][0]
    assert entry["content"].startswith(_HEADER + "\n\n")
    assert "body." in entry["content"]


def test_non_reference_drill_has_no_header(tmp_path: Path) -> None:
    _prepare(tmp_path)
    (tmp_path / "notes.md").write_text("# N\n\nplain file.\n", encoding="utf-8")

    out = asyncio.run(MemoryDrillTool(workspace=tmp_path).execute(uri="notes.md"))

    assert "plain file." in out["content"]
    assert "Entities distilled" not in out["content"]


def test_header_survives_the_loop_truncation_of_a_long_document(tmp_path: Path) -> None:
    """A reference longer than the loop's tool-result cap keeps its entity
    block: the loop truncates from the tail, so the block has to lead."""
    from durin.agent.loop import _truncate_tool_output

    cap = 4_000
    _prepare(tmp_path, body="# T\n\n" + ("filler paragraph. " * 1000) + "\n")

    out = asyncio.run(MemoryDrillTool(workspace=tmp_path).execute(
        uri="reference:thinking-fast-and-slow",
    ))
    assert len(out["content"]) > cap

    truncated = _truncate_tool_output(out["content"], cap, "memory_drill")
    assert truncated.startswith(_HEADER + "\n\n")
