import pytest
from durin.agent.tools.mcp_search import McpSearchTool
from durin.agent.mcp_catalog_cache import McpCatalogCache


@pytest.mark.asyncio
async def test_include_all_overrides_gate(tmp_path, monkeypatch):
    cat = tmp_path / "c.json"
    cache = McpCatalogCache(cat)
    cache._servers = [{"name": "io.github.x/y", "description": "github",
                       "_github": {"stars": 3, "owner_type": "User"}}]
    cache._path.write_text(  # persist so the tool's fresh cache loads it
        __import__("json").dumps({"servers": cache._servers, "meta": {}}))

    tool = McpSearchTool(cache_path=cat, registries=[], limit=10,
                         quality="official", min_stars=100)
    gated = await tool.execute(query="github")
    assert gated["hits"] == []
    widened = await tool.execute(query="github", include_all=True)
    assert len(widened["hits"]) == 1
