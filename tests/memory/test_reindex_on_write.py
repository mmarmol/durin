"""Re-index-on-write: every tool that writes a `.md` triggers a
synchronous FTS5 row update (doc 02 §6.1 + §6.2).

Without this hook, the FTS5 index ships empty in production — only
populated by manual `durin memory reindex`. The hook makes lexical
search useful out of the box.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from durin.memory.fts_index import FTSIndex


def test_memory_store_indexes_new_entry(tmp_path: Path) -> None:
    """After `MemoryStoreTool.execute`, the new entry is searchable
    via FTS5 — no manual reindex needed."""
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)
    asyncio.run(tool.execute(
        content="Marcelo Marmol is the architect of durin",
        entities=["person:marcelo"],
    ))

    with FTSIndex.open(tmp_path) as idx:
        assert idx.count() >= 1, (
            "memory_store wrote an entry but FTS5 has no rows — "
            "re-index-on-write hook missing"
        )
        hits = idx.search("Marcelo")
    assert hits, "Marcelo content not findable via FTS5"


def test_memory_ingest_indexes_chunks(tmp_path: Path) -> None:
    """`MemoryIngestTool` writes ingested chunks; each must land in FTS."""
    from durin.agent.tools.context import ToolContext
    from durin.agent.tools.memory_ingest import MemoryIngestTool

    # Create a source file on disk.
    src = tmp_path / "src.txt"
    src.write_text(
        "Marcelo Marmol founded durin in 2026. The agent has memory.",
        encoding="utf-8",
    )
    tool = MemoryIngestTool(workspace=tmp_path)
    asyncio.run(tool.execute(path=str(src)))

    with FTSIndex.open(tmp_path) as idx:
        assert idx.count() >= 1
        hits = idx.search("Marcelo")
    assert hits, "memory_ingest wrote chunks but FTS5 didn't see them"


def test_dream_apply_reindexes_entity_page(tmp_path: Path) -> None:
    """After Dream apply rewrites an entity page, FTS5 reflects the
    new content."""
    import json as _json

    from durin.memory.dream import DreamConsolidator, EntryRef

    ops = [
        {"op": "add", "path": "/aliases/-", "value": "marcelo",
         "provenance": "episodic/e1.md"},
        {"op": "add", "path": "/attributes/role",
         "value": "founder_of_durin",
         "provenance": "episodic/e1.md"},
    ]
    response = (
        "===PATCH===\n" + _json.dumps(ops, indent=2) + "\n"
        + "===BODY_DELTA===\nFounder of durin.\n"
        + "===COMMIT===\n"
        + "Consolidate person:marcelo (rev 1)\n\nfirst pass\n\n"
        + "Sources: episodic/e1.md\n"
        + "Cursor-after: 2026-05-23\n"
        + "Entities-touched: person:marcelo\n"
        + "===END===\n"
    )
    c = DreamConsolidator(
        workspace=tmp_path,
        llm_invoke=lambda p, *, model: response,
    )
    result = c.consolidate_entity(
        "person:marcelo",
        [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
    )
    c.apply("person:marcelo", result)

    with FTSIndex.open(tmp_path) as idx:
        hits = idx.search("founder_of_durin")
    assert any(h.uri == "person:marcelo" for h in hits), (
        "Dream apply wrote the entity page but FTS5 didn't pick up "
        "the attribute"
    )
