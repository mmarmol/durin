import pytest

from durin.agent.tools.mcp_search import McpSearchTool


@pytest.mark.asyncio
async def test_include_all_overrides_gate(monkeypatch):
    from durin.agent import mcp_catalog_store

    monkeypatch.setattr(mcp_catalog_store, "load_servers", lambda: [
        {"name": "io.github.x/y", "ref": "io.github.x/y",
         "description": "github", "stars": 3},
    ])

    tool = McpSearchTool(limit=10, quality="official", min_stars=100)
    gated = await tool.execute(query="github")
    assert gated["hits"] == []
    widened = await tool.execute(query="github", include_all=True)
    assert len(widened["hits"]) == 1
