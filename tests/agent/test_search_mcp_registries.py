"""Task 6 — adapter builder + store-backed search orchestration.

``search_mcp_registries`` now delegates to the durin-owned catalog store
(``durin.agent.mcp_catalog_store``); tests seed the store's ``load_servers``.
``build_mcp_adapters`` is still exercised here because INSTALL/describe use it.
"""
import pytest

from durin.agent.mcp_registry import build_mcp_adapters, search_mcp_registries
from durin.config.schema import McpRegistryConfig


def _seed(monkeypatch, servers):
    """Make the catalog store return ``servers`` from its loader."""
    from durin.agent import mcp_catalog_store

    monkeypatch.setattr(mcp_catalog_store, "load_servers", lambda: servers)


def test_build_adapters_enabled_official_plus_github():
    ads = build_mcp_adapters([
        McpRegistryConfig(name="official", kind="official"),
        McpRegistryConfig(name="mpak", kind="mpak", enabled=False),
    ])
    # official is built (mpak disabled → skipped); github's curated registry is always
    # appended as the verified tier + install fallback (public API, no token).
    assert [a.name for a in ads] == ["official", "github"]


@pytest.mark.asyncio
async def test_search_ranks_from_store(monkeypatch):
    _seed(monkeypatch, [
        {"name": "io.x/jira", "ref": "io.x/jira", "description": "Jira",
         "official": True},
    ])
    hits = await search_mcp_registries("jira", limit=5)
    assert hits[0].ref == "io.x/jira"


@pytest.mark.asyncio
async def test_search_forwards_quality(monkeypatch):
    _seed(monkeypatch, [
        {"name": "io.github.x/y", "ref": "io.github.x/y",
         "description": "github thing", "stars": 3},
    ])
    # default (official) gate hides the low-star, non-official server...
    assert await search_mcp_registries("github", limit=5) == []
    # ...quality="all" returns it
    hits = await search_mcp_registries("github", limit=5, quality="all")
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_search_forwards_min_stars(monkeypatch):
    _seed(monkeypatch, [
        {"name": "io.x/postgres", "ref": "io.x/postgres",
         "description": "postgres", "stars": 250},
    ])
    # min_stars above the server's star count → gated out
    assert await search_mcp_registries("postgres", limit=5, min_stars=500) == []
    # min_stars below it → returned
    hits = await search_mcp_registries("postgres", limit=5, min_stars=100)
    assert hits[0].ref == "io.x/postgres"
