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
