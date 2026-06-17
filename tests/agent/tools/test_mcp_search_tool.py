"""Phase 2 / Task 8 — mcp_search agent tool."""
import pytest

from durin.agent.tools.mcp_search import McpSearchTool


class _Reg:
    name = "official"

    async def fetch_page(self, *, cursor=None, updated_since=None):
        return [{"name": "io.x/jira", "description": "Jira"}], None


@pytest.mark.asyncio
async def test_mcp_search_returns_hits(tmp_path, monkeypatch):
    import durin.agent.tools.mcp_search as m

    monkeypatch.setattr(m, "build_mcp_adapters", lambda regs: [_Reg()])
    tool = McpSearchTool(cache_path=tmp_path / "c.json", registries=[], limit=10)
    out = await tool.execute(query="jira")
    assert out["hits"][0]["ref"] == "io.x/jira"
    assert tool.read_only is True
    assert tool.name == "mcp_search"


@pytest.mark.asyncio
async def test_mcp_search_empty_query_errors(tmp_path):
    tool = McpSearchTool(cache_path=tmp_path / "c.json", registries=[], limit=10)
    out = await tool.execute(query="")
    assert "error" in out


def test_mcp_search_is_discoverable():
    import durin.agent.tools as tools_pkg
    from durin.agent.tools.loader import ToolLoader

    classes = ToolLoader(tools_pkg).discover()
    assert any(c.__name__ == "McpSearchTool" for c in classes)
