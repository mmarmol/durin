"""Tests for durin.agent.mcp_catalog_refresh.

TDD order: write failing tests first, implement to make them pass.

Mirrors tests/providers/test_catalog_refresh.py + covers the newer-than guard
and cache_clear() contract specific to the MCP catalog.
"""

from __future__ import annotations

import json

import durin.agent.mcp_catalog_store as store
from durin.agent import mcp_catalog_refresh as cr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FLOOR_TS = "2026-06-19T00:00:00Z"
OLDER_TS = "2026-06-18T00:00:00Z"
NEWER_TS = "2026-06-20T00:00:00Z"

_FAKE_CATALOG = {
    "schema_version": 1,
    "generated_at": NEWER_TS,
    "servers": [
        {
            "name": "test-server",
            "ref": "test-owner/test-server",
            "description": "A test MCP server",
        }
    ],
}


def _make_fetch(payload: dict):
    """Return a fake fetch callable that returns JSON bytes."""

    def _fetch(url: str) -> bytes:
        return json.dumps(payload).encode("utf-8")

    return _fetch


def _make_failing_fetch(exc=OSError("offline")):
    """Return a fake fetch callable that raises."""

    def _fetch(url: str):
        raise exc

    return _fetch


# ---------------------------------------------------------------------------
# refresh_catalog: writes overlay when remote is strictly newer
# ---------------------------------------------------------------------------


def test_refresh_writes_overlay_when_remote_is_newer(tmp_path, monkeypatch):
    """When the remote generated_at is strictly newer than the local floor, the
    overlay mcp_catalog_cache.json is written and the function returns True."""
    # Floor has FLOOR_TS (2026-06-19); we send NEWER_TS (2026-06-20)
    result = cr.refresh_catalog(tmp_path, url="http://fake/catalog.json", fetch=_make_fetch(_FAKE_CATALOG))

    assert result is True
    cache = tmp_path / "mcp_catalog_cache.json"
    assert cache.exists()
    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written["generated_at"] == NEWER_TS
    assert written["servers"][0]["name"] == "test-server"


def test_refresh_returns_true_and_clears_cache(tmp_path, monkeypatch):
    """On success, cache_clear() is invoked on mcp_catalog_store."""
    cleared = []
    monkeypatch.setattr(store, "cache_clear", lambda: cleared.append(1))

    result = cr.refresh_catalog(tmp_path, url="http://fake/catalog.json", fetch=_make_fetch(_FAKE_CATALOG))

    assert result is True
    assert len(cleared) == 1


# ---------------------------------------------------------------------------
# refresh_catalog: does NOT write when remote is older or equal
# ---------------------------------------------------------------------------


def test_refresh_skips_write_when_remote_is_older(tmp_path, monkeypatch):
    """When the remote generated_at is older than the local floor, the overlay
    is NOT written and the function returns False."""
    old_catalog = dict(_FAKE_CATALOG, generated_at=OLDER_TS)
    result = cr.refresh_catalog(tmp_path, url="http://fake/catalog.json", fetch=_make_fetch(old_catalog))

    assert result is False
    assert not (tmp_path / "mcp_catalog_cache.json").exists()


def test_refresh_skips_write_when_remote_ts_equal_to_floor(tmp_path, monkeypatch):
    """When the remote generated_at equals the floor's, skip write (not strictly newer)."""
    equal_catalog = dict(_FAKE_CATALOG, generated_at=FLOOR_TS)
    result = cr.refresh_catalog(tmp_path, url="http://fake/catalog.json", fetch=_make_fetch(equal_catalog))

    assert result is False
    assert not (tmp_path / "mcp_catalog_cache.json").exists()


def test_refresh_skips_write_when_remote_ts_equal_to_overlay(tmp_path, monkeypatch):
    """When the overlay already exists with the same timestamp, skip write."""
    # Pre-write an overlay with NEWER_TS
    overlay = tmp_path / "mcp_catalog_cache.json"
    overlay.write_text(json.dumps(_FAKE_CATALOG), encoding="utf-8")

    same_ts_catalog = dict(_FAKE_CATALOG, generated_at=NEWER_TS)
    mtime_before = overlay.stat().st_mtime

    result = cr.refresh_catalog(tmp_path, url="http://fake/catalog.json", fetch=_make_fetch(same_ts_catalog))

    assert result is False
    # File untouched
    assert overlay.stat().st_mtime == mtime_before


# ---------------------------------------------------------------------------
# refresh_catalog: fetch/parse failures → return False, no raise
# ---------------------------------------------------------------------------


def test_refresh_returns_false_on_fetch_failure(tmp_path):
    """Network error → returns False, no overlay written, no exception raised."""
    result = cr.refresh_catalog(tmp_path, url="http://fake/catalog.json", fetch=_make_failing_fetch())

    assert result is False
    assert not (tmp_path / "mcp_catalog_cache.json").exists()


def test_refresh_returns_false_on_bad_json(tmp_path):
    """Corrupt response → returns False, no overlay written, no exception raised."""

    def _bad_fetch(url: str) -> bytes:
        return b"not-json!!!"

    result = cr.refresh_catalog(tmp_path, url="http://fake/catalog.json", fetch=_bad_fetch)

    assert result is False
    assert not (tmp_path / "mcp_catalog_cache.json").exists()


def test_refresh_returns_false_on_missing_servers_key(tmp_path):
    """Valid JSON but missing 'servers' key → returns False."""

    def _fetch(url: str) -> bytes:
        return json.dumps({"generated_at": NEWER_TS, "no_servers": []}).encode()

    result = cr.refresh_catalog(tmp_path, url="http://fake/catalog.json", fetch=_fetch)

    assert result is False
    assert not (tmp_path / "mcp_catalog_cache.json").exists()


def test_refresh_prior_overlay_preserved_on_fetch_failure(tmp_path):
    """Existing overlay is kept untouched when the fetch fails."""
    overlay = tmp_path / "mcp_catalog_cache.json"
    overlay.write_text(json.dumps(_FAKE_CATALOG), encoding="utf-8")
    mtime_before = overlay.stat().st_mtime

    result = cr.refresh_catalog(tmp_path, url="http://fake/catalog.json", fetch=_make_failing_fetch())

    assert result is False
    assert overlay.exists()
    assert overlay.stat().st_mtime == mtime_before


# ---------------------------------------------------------------------------
# Overlay wins when newer: verifiable via load_servers
# ---------------------------------------------------------------------------


def test_refresh_overlay_picked_up_by_load_servers(tmp_path, monkeypatch):
    """After a successful refresh, load_servers() returns the new overlay servers."""
    import durin.agent.mcp_catalog_store as mcs

    # Point the overlay path at tmp_path
    monkeypatch.setattr(mcs, "_overlay_path", lambda: tmp_path / "mcp_catalog_cache.json")

    result = cr.refresh_catalog(tmp_path, url="http://fake/catalog.json", fetch=_make_fetch(_FAKE_CATALOG))
    assert result is True

    mcs.cache_clear()
    servers = mcs.load_servers()
    assert any(s["name"] == "test-server" for s in servers)
    mcs.cache_clear()  # cleanup


# ---------------------------------------------------------------------------
# McpCatalogRefreshScheduler: start/stop is safe and non-blocking
# ---------------------------------------------------------------------------


def test_scheduler_start_stop_no_hang(tmp_path):
    """start() then stop() completes quickly without hanging."""
    # Use a very large interval so the thread never actually fires
    sched = cr.McpCatalogRefreshScheduler(
        tmp_path, url="http://fake/catalog.json", interval_hours=99999
    )
    sched.start()
    sched.stop()  # Must return promptly


def test_scheduler_start_idempotent(tmp_path):
    """Calling start() twice does not create a second thread."""
    sched = cr.McpCatalogRefreshScheduler(
        tmp_path, url="http://fake/catalog.json", interval_hours=99999
    )
    sched.start()
    thread_before = sched._thread
    sched.start()  # Second call should be a no-op
    assert sched._thread is thread_before
    sched.stop()


def test_scheduler_stop_before_start_is_safe(tmp_path):
    """stop() before start() does not raise."""
    sched = cr.McpCatalogRefreshScheduler(
        tmp_path, url="http://fake/catalog.json", interval_hours=99999
    )
    sched.stop()  # Should not raise
