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
