"""Task 6 — adapter builder + cache-backed search orchestration."""
import pytest

from durin.agent.mcp_catalog_cache import McpCatalogCache
from durin.agent.mcp_registry import build_mcp_adapters, search_mcp_registries
from durin.config.schema import McpRegistryConfig


class _Reg:
    name = "official"

    async def fetch_page(self, *, cursor=None, updated_since=None):
        return [{"name": "io.x/jira", "description": "Jira"}], None

    async def search(self, query, *, limit):
        from durin.agent.mcp_registry import _hit_from_server

        servers, _ = await self.fetch_page()
        return [_hit_from_server(s, registry="official") for s in servers][:limit]


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


@pytest.mark.asyncio
async def test_background_sync_survives_and_populates_cache(tmp_path):
    """Guards the GC fix: the background catalog sync must run to completion and
    persist the catalog to disk. If the task were GC-collected mid-run (the
    fire-and-forget footgun), the on-disk cache would stay empty and this fails."""
    import asyncio

    from durin.agent.mcp_registry import _BACKGROUND_TASKS

    cache = McpCatalogCache(tmp_path / "c.json")
    await search_mcp_registries("jira", cache=cache, adapters=[_Reg()], limit=5)
    await asyncio.sleep(0)
    if _BACKGROUND_TASKS:
        await asyncio.gather(*list(_BACKGROUND_TASKS))
    # a fresh cache loads the catalog the background task wrote to disk
    assert McpCatalogCache(tmp_path / "c.json")._servers


@pytest.mark.asyncio
async def test_search_forwards_quality(tmp_path):
    cache = McpCatalogCache(tmp_path / "c.json")
    cache._servers = [
        {"name": "io.github.x/y", "description": "github thing",
         "_github": {"stars": 3, "owner_type": "User"}},
    ]
    # default (official) gate hides the low-star server...
    assert await search_mcp_registries("github", cache=cache, adapters=[], limit=5) == []
    # ...quality="all" returns it
    hits = await search_mcp_registries("github", cache=cache, adapters=[], limit=5,
                                       quality="all")
    assert len(hits) == 1


def test_build_enricher_none_without_token():
    from durin.agent.mcp_registry import _build_enricher

    assert _build_enricher(None) is None


def test_build_enricher_returns_callable(monkeypatch):
    from durin.agent.mcp_registry import _build_enricher
    import durin.agent.mcp_github as ghmod

    captured = {}

    def fake_fetch(repo_keys, *, token, **kw):
        captured["token"] = token
        return {("github", "x"): ghmod.GithubMeta(stars=5)}

    monkeypatch.setattr(ghmod, "fetch_repo_meta", fake_fetch)
    enrich = _build_enricher("tok")
    out = enrich([("github", "x")])
    assert captured["token"] == "tok"
    assert out[("github", "x")].stars == 5
