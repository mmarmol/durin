"""Task 6 — adapter builder + cache-backed search orchestration."""
import pytest

from durin.agent.mcp_catalog_cache import McpCatalogCache
from durin.agent.mcp_registry import build_mcp_adapters, search_mcp_registries
from durin.config.schema import McpRegistryConfig


class _Reg:
    name = "official"

    async def fetch_page(self, *, cursor=None, updated_since=None):
        return [{"name": "io.x/jira", "description": "Jira"}], None


def test_build_adapters_only_enabled_official():
    ads = build_mcp_adapters([
        McpRegistryConfig(name="official", kind="official"),
        McpRegistryConfig(name="mpak", kind="mpak", enabled=False),
    ])
    assert [a.name for a in ads] == ["official"]


@pytest.mark.asyncio
async def test_search_syncs_empty_cache_then_ranks(tmp_path):
    cache = McpCatalogCache(tmp_path / "c.json")
    hits = await search_mcp_registries("jira", cache=cache, adapters=[_Reg()], limit=5)
    assert hits[0].ref == "io.x/jira"
