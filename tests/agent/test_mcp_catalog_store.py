"""Tests for mcp_catalog_store: floor + overlay loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


FLOOR_SERVERS_COUNT = 5  # mcp_catalog.json currently has 5 entries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_overlay(path: Path, servers: list[dict], generated_at: str) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "generated_at": generated_at, "servers": servers}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLoadServers:
    def test_floor_only_returns_floor_servers(self, monkeypatch):
        """No overlay present → returns the 5 vendored floor servers."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "_overlay_path", lambda: None)
        mcp_catalog_store.cache_clear()

        servers = mcp_catalog_store.load_servers()

        assert len(servers) == FLOOR_SERVERS_COUNT

    def test_overlay_newer_wins(self, tmp_path, monkeypatch):
        """Overlay with generated_at > floor → overlay servers returned."""
        from durin.agent import mcp_catalog_store

        overlay_file = tmp_path / "mcp_catalog_cache.json"
        _write_overlay(
            overlay_file,
            servers=[{"name": "overlay-server", "ref": "overlay/server"}],
            generated_at="2099-01-01T00:00:00Z",
        )
        monkeypatch.setattr(mcp_catalog_store, "_overlay_path", lambda: overlay_file)
        mcp_catalog_store.cache_clear()

        servers = mcp_catalog_store.load_servers()

        assert len(servers) == 1
        assert servers[0]["name"] == "overlay-server"

    def test_overlay_same_timestamp_as_floor_wins(self, tmp_path, monkeypatch):
        """Overlay with generated_at == floor's → overlay still wins (>= comparison)."""
        from durin.agent import mcp_catalog_store

        overlay_file = tmp_path / "mcp_catalog_cache.json"
        # Floor timestamp is 2026-06-19T00:00:00Z — match it exactly
        _write_overlay(
            overlay_file,
            servers=[{"name": "equal-ts-server", "ref": "eq/server"}],
            generated_at="2026-06-19T00:00:00Z",
        )
        monkeypatch.setattr(mcp_catalog_store, "_overlay_path", lambda: overlay_file)
        mcp_catalog_store.cache_clear()

        servers = mcp_catalog_store.load_servers()

        assert servers[0]["name"] == "equal-ts-server"

    def test_overlay_older_falls_back_to_floor(self, tmp_path, monkeypatch):
        """Overlay with generated_at < floor → floor servers used, no exception."""
        from durin.agent import mcp_catalog_store

        overlay_file = tmp_path / "mcp_catalog_cache.json"
        _write_overlay(
            overlay_file,
            servers=[{"name": "stale-server"}],
            generated_at="2000-01-01T00:00:00Z",
        )
        monkeypatch.setattr(mcp_catalog_store, "_overlay_path", lambda: overlay_file)
        mcp_catalog_store.cache_clear()

        servers = mcp_catalog_store.load_servers()

        assert len(servers) == FLOOR_SERVERS_COUNT

    def test_corrupt_overlay_falls_back_to_floor(self, tmp_path, monkeypatch):
        """Corrupt JSON in overlay → falls back to floor, no exception raised."""
        from durin.agent import mcp_catalog_store

        overlay_file = tmp_path / "mcp_catalog_cache.json"
        overlay_file.write_text("NOT VALID JSON", encoding="utf-8")
        monkeypatch.setattr(mcp_catalog_store, "_overlay_path", lambda: overlay_file)
        mcp_catalog_store.cache_clear()

        servers = mcp_catalog_store.load_servers()

        assert len(servers) == FLOOR_SERVERS_COUNT

    def test_missing_overlay_path_returns_floor(self, monkeypatch):
        """_overlay_path() returning None → floor used gracefully."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "_overlay_path", lambda: None)
        mcp_catalog_store.cache_clear()

        servers = mcp_catalog_store.load_servers()

        assert len(servers) == FLOOR_SERVERS_COUNT

    def test_cache_clear_picks_up_new_overlay(self, tmp_path, monkeypatch):
        """cache_clear() lets a newly written overlay take effect."""
        from durin.agent import mcp_catalog_store

        overlay_file = tmp_path / "mcp_catalog_cache.json"
        monkeypatch.setattr(mcp_catalog_store, "_overlay_path", lambda: overlay_file)

        # First call: no overlay → floor
        mcp_catalog_store.cache_clear()
        servers_before = mcp_catalog_store.load_servers()
        assert len(servers_before) == FLOOR_SERVERS_COUNT

        # Write overlay, clear cache
        _write_overlay(
            overlay_file,
            servers=[{"name": "fresh-server"}],
            generated_at="2099-01-01T00:00:00Z",
        )
        mcp_catalog_store.cache_clear()

        servers_after = mcp_catalog_store.load_servers()
        assert servers_after[0]["name"] == "fresh-server"


# ---------------------------------------------------------------------------
# Tests for search()
# ---------------------------------------------------------------------------

def _fake_servers() -> list[dict]:
    return [
        {
            "name": "github-mcp",
            "ref": "github/github-mcp-server",
            "description": "GitHub integration for MCP",
            "kind": "local",
            "stars": 5000,
            "official": True,
            "owner_login": "github",
            "owner_url": "https://github.com/github",
            "owner_avatar": "",
            "topics": ["github"],
            "language": "Go",
            "license": "MIT",
            "repo_url": "https://github.com/github/github-mcp-server",
        },
        {
            "name": "postgres-mcp",
            "ref": "acme/postgres-mcp",
            "description": "PostgreSQL database access",
            "kind": "local",
            "stars": 500,
            "official": False,
            "owner_login": "acme",
            "owner_url": "https://github.com/acme",
            "owner_avatar": "",
            "topics": ["postgres"],
            "language": "Python",
            "license": "Apache-2.0",
            "repo_url": "https://github.com/acme/postgres-mcp",
        },
        {
            "name": "lowstar-mcp",
            "ref": "nobody/lowstar-mcp",
            "description": "Low star non-official server",
            "kind": "local",
            "stars": 5,
            "official": False,
            "owner_login": "nobody",
            "owner_url": "https://github.com/nobody",
            "owner_avatar": "",
            "topics": [],
            "language": "JavaScript",
            "license": "MIT",
            "repo_url": "https://github.com/nobody/lowstar-mcp",
        },
        {
            "name": "official-lowstar-mcp",
            "ref": "official/lowstar-mcp",
            "description": "Official but low star server",
            "kind": "remote",
            "stars": 10,
            "official": True,
            "owner_login": "official",
            "owner_url": "https://github.com/official",
            "owner_avatar": "",
            "topics": [],
            "language": "TypeScript",
            "license": "MIT",
            "repo_url": "https://github.com/official/lowstar-mcp",
        },
    ]


class TestSearch:
    def test_gate_excludes_low_star_non_official(self, monkeypatch):
        """Default quality gate excludes stars<=100 and official=False servers."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "load_servers", _fake_servers)

        results = mcp_catalog_store.search("mcp", limit=10)
        names = [r.name for r in results]

        assert "lowstar-mcp" not in names

    def test_gate_includes_high_star_server(self, monkeypatch):
        """Default quality gate includes servers with stars > min_stars."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "load_servers", _fake_servers)

        results = mcp_catalog_store.search("mcp", limit=10)
        names = [r.name for r in results]

        assert "postgres-mcp" in names

    def test_gate_includes_official_low_star(self, monkeypatch):
        """Default quality gate includes official=True servers regardless of stars."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "load_servers", _fake_servers)

        results = mcp_catalog_store.search("mcp", limit=10)
        names = [r.name for r in results]

        assert "official-lowstar-mcp" in names

    def test_star_sort_higher_stars_first(self, monkeypatch):
        """Results sorted by stars descending; github-mcp (5000) before postgres-mcp (500)."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "load_servers", _fake_servers)

        results = mcp_catalog_store.search("mcp", limit=10)
        names = [r.name for r in results]

        assert names.index("github-mcp") < names.index("postgres-mcp")

    def test_signals_carried(self, monkeypatch):
        """Top hit's signals dict contains stars and official; kind matches source dict."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "load_servers", _fake_servers)

        results = mcp_catalog_store.search("github", limit=1)

        assert len(results) == 1
        hit = results[0]
        assert hit.signals["stars"] == 5000
        assert hit.signals["official"] is True
        assert hit.kind == "local"

    def test_quality_all_includes_low_star_non_official(self, monkeypatch):
        """quality='all' skips the gate and returns low-star non-official servers."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "load_servers", _fake_servers)

        results = mcp_catalog_store.search("lowstar", limit=10, quality="all")
        names = [r.name for r in results]

        assert "lowstar-mcp" in names


# ---------------------------------------------------------------------------
# Floor-catalog relevance: substring/typo matching, not loose namespace fuzzy
# ---------------------------------------------------------------------------

def _floor_like_servers() -> list[dict]:
    """Mirrors the structure of mcp_catalog.json floor (io.github.* refs)."""
    return [
        {
            "name": "io.github.github/github-mcp-server",
            "ref": "io.github.github/github-mcp-server",
            "description": "Manage GitHub repositories, issues, pull requests and Actions.",
            "kind": "both",
            "stars": 30000,
            "official": True,
            "owner_login": "github",
            "topics": [],
            "language": "Go",
            "license": "",
            "owner_url": "",
            "owner_avatar": "",
            "repo_url": "",
        },
        {
            "name": "io.github.ChromeDevTools/chrome-devtools-mcp",
            "ref": "io.github.ChromeDevTools/chrome-devtools-mcp",
            "description": "Control and inspect Chrome via the DevTools Protocol for browser automation.",
            "kind": "local",
            "stars": 43000,
            "official": True,
            "owner_login": "ChromeDevTools",
            "topics": [],
            "language": "TypeScript",
            "license": "",
            "owner_url": "",
            "owner_avatar": "",
            "repo_url": "",
        },
        {
            "name": "io.github.microsoft/playwright-mcp",
            "ref": "io.github.microsoft/playwright-mcp",
            "description": "Drive a real browser via Playwright.",
            "kind": "local",
            "stars": 34000,
            "official": True,
            "owner_login": "microsoft",
            "topics": [],
            "language": "TypeScript",
            "license": "",
            "owner_url": "",
            "owner_avatar": "",
            "repo_url": "",
        },
        {
            "name": "com.microsoft/azure",
            "ref": "com.microsoft/azure",
            "description": "Manage and query Azure resources.",
            "kind": "both",
            "stars": 3000,
            "official": True,
            "owner_login": "microsoft",
            "topics": [],
            "language": "",
            "license": "",
            "owner_url": "",
            "owner_avatar": "",
            "repo_url": "",
        },
    ]


class TestSearchRelevance:
    def test_nada_returns_empty(self, monkeypatch):
        """'nada' fuzzy-matches nothing — avoids loose whole-string ratio hits."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "load_servers", _floor_like_servers)

        results = mcp_catalog_store.search("nada", limit=10, quality="all")

        assert results == []

    def test_github_matches_only_github_server(self, monkeypatch):
        """'github' matches the github-mcp-server only, not chrome-devtools/playwright/azure."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "load_servers", _floor_like_servers)

        results = mcp_catalog_store.search("github", limit=10, quality="all")
        refs = [r.ref for r in results]

        assert "io.github.github/github-mcp-server" in refs
        assert not any(
            r in refs
            for r in [
                "io.github.ChromeDevTools/chrome-devtools-mcp",
                "io.github.microsoft/playwright-mcp",
                "com.microsoft/azure",
            ]
        ), f"Unexpected refs matched 'github': {refs}"

    def test_playwright_matches_playwright(self, monkeypatch):
        """'playwright' substring of name segment → matches playwright-mcp only."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "load_servers", _floor_like_servers)

        results = mcp_catalog_store.search("playwright", limit=10, quality="all")
        refs = [r.ref for r in results]

        assert "io.github.microsoft/playwright-mcp" in refs
        assert "io.github.ChromeDevTools/chrome-devtools-mcp" not in refs

    def test_browser_matches_via_description(self, monkeypatch):
        """'browser' is in chrome-devtools description and playwright description → both match."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "load_servers", _floor_like_servers)

        results = mcp_catalog_store.search("browser", limit=10, quality="all")
        refs = [r.ref for r in results]

        assert "io.github.ChromeDevTools/chrome-devtools-mcp" in refs

    def test_azure_matches_via_description(self, monkeypatch):
        """'azure' is in the Azure server description → matches it."""
        from durin.agent import mcp_catalog_store

        monkeypatch.setattr(mcp_catalog_store, "load_servers", _floor_like_servers)

        results = mcp_catalog_store.search("azure", limit=10, quality="all")
        refs = [r.ref for r in results]

        assert "com.microsoft/azure" in refs
