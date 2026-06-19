"""Phase 2 / Task 8 — mcp_search agent tool."""
import pytest

from durin.agent.tools.mcp_search import McpSearchTool


@pytest.mark.asyncio
async def test_mcp_search_returns_hits(monkeypatch):
    from durin.agent import mcp_catalog_store

    monkeypatch.setattr(mcp_catalog_store, "load_servers", lambda: [
        {"name": "io.x/jira", "ref": "io.x/jira", "description": "Jira",
         "official": True},
    ])
    tool = McpSearchTool(limit=10)
    out = await tool.execute(query="jira")
    assert out["hits"][0]["ref"] == "io.x/jira"
    assert tool.read_only is True
    assert tool.name == "mcp_search"


@pytest.mark.asyncio
async def test_mcp_search_empty_query_errors():
    tool = McpSearchTool(limit=10)
    out = await tool.execute(query="")
    assert "error" in out


def test_mcp_search_is_discoverable():
    import durin.agent.tools as tools_pkg
    from durin.agent.tools.loader import ToolLoader

    classes = ToolLoader(tools_pkg).discover()
    assert any(c.__name__ == "McpSearchTool" for c in classes)
