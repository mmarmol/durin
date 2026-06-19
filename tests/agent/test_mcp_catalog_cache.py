"""Task 5 — local catalog cache + fuzzy ranking."""
import pytest

from durin.agent.mcp_catalog_cache import McpCatalogCache
from durin.agent.mcp_github import GithubMeta


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


class _FakeRegistry:
    async def fetch_page(self, *, cursor=None, updated_since=None):
        return [
            {"name": "com.stripe/mcp", "description": "pay",
             "repository": {"url": "https://github.com/stripe/agent-toolkit"}},
        ], None


@pytest.mark.asyncio
async def test_sync_attaches_github_meta(tmp_path):
    cache = McpCatalogCache(tmp_path / "cat.json")

    def enrich(repo_keys):
        assert ("stripe", "agent-toolkit") in repo_keys
        return {("stripe", "agent-toolkit"): GithubMeta(stars=900, owner_login="stripe",
                owner_type="Organization", owner_url="https://github.com/stripe")}

    n = await cache.sync(_FakeRegistry(), enrich=enrich)
    assert n == 1
    srv = cache._servers[0]
    assert srv["_github"]["stars"] == 900
    assert srv["_github"]["owner_login"] == "stripe"
