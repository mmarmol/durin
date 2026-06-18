"""Task 5 — local catalog cache + fuzzy ranking."""
import pytest

from durin.agent.mcp_catalog_cache import McpCatalogCache


class _Reg:
    name = "official"

    def __init__(self, servers):
        self._servers = servers

    async def fetch_page(self, *, cursor=None, updated_since=None):
        return self._servers, None  # single page


@pytest.mark.asyncio
async def test_sync_then_fuzzy_rank(tmp_path):
    cache = McpCatalogCache(tmp_path / "catalog.json")
    n = await cache.sync(_Reg([
        {"name": "io.x/jira-cloud", "description": "Atlassian Jira issues"},
        {"name": "io.x/postgres", "description": "Query a Postgres database"},
    ]))
    assert n == 2
    # "database" is not in the jira name; fuzzy over the description surfaces postgres
    assert cache.rank("database", limit=5)[0].ref == "io.x/postgres"
    # exact substring "jira" ranks the jira server first
    assert cache.rank("jira", limit=5)[0].ref == "io.x/jira-cloud"


@pytest.mark.asyncio
async def test_cache_persists_across_instances(tmp_path):
    p = tmp_path / "catalog.json"
    await McpCatalogCache(p).sync(_Reg([{"name": "io.x/a", "description": "alpha tool"}]))
    # a fresh instance loads the cached catalog from disk, no sync needed
    assert McpCatalogCache(p).rank("alpha", limit=5)[0].ref == "io.x/a"
