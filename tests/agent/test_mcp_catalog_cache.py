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


def _srv(name, desc, stars, owner_type="User"):
    return {"name": name, "description": desc,
            "_github": {"stars": stars, "owner_type": owner_type, "owner_login": "x"}}


@pytest.mark.asyncio
async def test_rank_gate_and_sort(tmp_path):
    cache = McpCatalogCache(tmp_path / "c.json")
    cache._servers = [
        _srv("io.github.a/playwright-mini", "playwright clone", 5),       # excluded (<100, not official)
        _srv("io.github.microsoft/playwright-mcp", "playwright", 34000, "Organization"),  # kept
        _srv("com.acme/playwright", "playwright vendor", 3, "Organization"),  # kept (official vendor domain)
    ]
    hits = cache.rank("playwright", limit=10, quality="official", min_stars=100)
    refs = [h.ref for h in hits]
    assert "io.github.a/playwright-mini" not in refs
    assert refs[0] == "io.github.microsoft/playwright-mcp"  # most stars first
    assert "com.acme/playwright" in refs
    # signals carried
    top = hits[0]
    assert top.signals.get("stars") == 34000
    assert top.signals.get("official") is True


@pytest.mark.asyncio
async def test_rank_all_disables_gate(tmp_path):
    cache = McpCatalogCache(tmp_path / "c.json")
    cache._servers = [_srv("io.github.a/playwright-mini", "playwright clone", 5)]
    hits = cache.rank("playwright", limit=10, quality="all", min_stars=100)
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_rank_unenriched_disables_gate(tmp_path):
    """When no server has _github stars (unenriched catalog), quality gate must be
    disabled so tokenless users still get results instead of near-empty results."""
    cache = McpCatalogCache(tmp_path / "c.json")
    # Unenriched: _github is absent or empty — no stars data at all
    cache._servers = [
        {"name": "io.github.foo/bar", "description": "playwright tool"},
    ]
    hits = cache.rank("playwright", limit=10, quality="official", min_stars=100)
    # Gate must be disabled because catalog is unenriched; server must be returned
    assert len(hits) == 1
    assert hits[0].ref == "io.github.foo/bar"
