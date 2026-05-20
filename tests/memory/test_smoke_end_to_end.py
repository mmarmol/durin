"""Phase-1 end-to-end smoke test.

Exercises the full pipeline a user-facing turn might walk through:

    ingest a doc → drill into a section → store a derived memory →
    search finds it → hot layer surfaces it.

Each individual tool has its own focused test suite. This module
verifies the modules compose correctly across the boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.agent.tools.memory_drill import MemoryDrillTool
from durin.agent.tools.memory_ingest import MemoryIngestTool
from durin.agent.tools.memory_search import MemorySearchTool
from durin.agent.tools.memory_store import MemoryStoreTool
from durin.memory import (
    author_scope,
    drill,
    ingest_artifact,
    read_hot_layer,
    search_memory,
    store_memory,
)


# ---------------------------------------------------------------------------
# pure-function flow
# ---------------------------------------------------------------------------


def test_phase_1_pure_function_chain(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"

    # 1) Ingest a document the user wants the agent to remember.
    src = tmp_path / "incoming.md"
    src.write_text(
        "# Topic\n"
        "\n"
        "## section-a\n"
        "important fact about the cache layer\n"
        "\n"
        "## section-b\n"
        "unrelated content\n",
        encoding="utf-8",
    )
    ingest = ingest_artifact(workspace, src)

    # 2) Drill into the specific section the agent wants to quote.
    section = drill(workspace, f"ingested/{ingest['id']}/source.md#section-a")
    assert "important fact about the cache layer" in section
    assert "unrelated content" not in section

    # 3) Store a derived learning that references the section.
    with author_scope("agent_created"):
        stored = store_memory(
            workspace,
            content="Cache must be flushed when payload version changes.",
            headline="Cache flush rule",
            class_name="stable",
            source_refs=[
                f"[section-a](../ingested/{ingest['id']}/source.md#section-a)"
            ],
            entities=["cache", "payload"],
        )
    assert stored["author"] == "agent_created"

    # 4) Search surfaces the new memory entry.
    results = search_memory(workspace, "cache", scope="all", level="warm")
    headlines = [r.headline for r in results if r.source == "memory"]
    assert "Cache flush rule" in headlines

    # 5) The hot layer carries the headline + entity into the stable prompt tier.
    hot = read_hot_layer(workspace)
    assert "Cache flush rule" in hot.headlines
    assert "cache" in hot.entities
    rendered = hot.render()
    assert "## Memory: Key Points" in rendered
    assert "Cache flush rule" in rendered


# ---------------------------------------------------------------------------
# tool-level chain (mirrors what the agent invokes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_1_tool_chain(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    src = tmp_path / "doc.md"
    src.write_text(
        "## intro\n"
        "context paragraph\n"
        "\n"
        "## api\n"
        "user prefers pytest over unittest\n",
        encoding="utf-8",
    )

    ingest_tool = MemoryIngestTool(workspace=workspace)
    store_tool = MemoryStoreTool(workspace=workspace)
    search_tool = MemorySearchTool(workspace=workspace)
    drill_tool = MemoryDrillTool(workspace=workspace)

    ingest_out = await ingest_tool.execute(path=str(src))
    assert "error" not in ingest_out
    assert "user prefers pytest over unittest" in ingest_out["content"]

    store_out = await store_tool.execute(
        content="User prefers pytest over unittest.",
        class_name="stable",
        headline="Testing preference",
        entities=["pytest", "unittest"],
    )
    assert store_out["author"] == "agent_created"

    search_out = await search_tool.execute(query="pytest", scope="all")
    assert search_out["total"] >= 1
    # At least one result is the dreamed memory we just stored.
    sources = {r["source"] for r in search_out["results"]}
    assert "memory" in sources

    # Drill into the ingested section by URI.
    drill_uri = f"ingested/{ingest_out['id']}/source.md#api"
    drill_out = await drill_tool.execute(uri=drill_uri)
    assert "error" not in drill_out
    assert "user prefers pytest" in drill_out["content"]
    assert "context paragraph" not in drill_out["content"]
