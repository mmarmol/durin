"""Single-process atomic+lock tests for mcp_catalog_cache.json."""

from __future__ import annotations

import json
from pathlib import Path


def _fake_fetch(payload: dict):
    def fetch(url: str) -> bytes:
        return json.dumps(payload).encode("utf-8")
    return fetch


def test_refresh_creates_lock_file(tmp_path: Path) -> None:
    from durin.agent.mcp_catalog_refresh import refresh_catalog

    payload = {
        "generated_at": "2099-01-01T00:00:00Z",
        "servers": [{"id": "test-server"}],
    }
    refresh_catalog(tmp_path, fetch=_fake_fetch(payload))
    cache = tmp_path / "mcp_catalog_cache.json"
    assert cache.exists()
    assert Path(f"{cache}.lock").exists()


def test_refresh_cache_is_valid_json(tmp_path: Path) -> None:
    from durin.agent.mcp_catalog_refresh import refresh_catalog

    payload = {
        "generated_at": "2099-01-02T00:00:00Z",
        "servers": [{"id": "srv-1"}],
    }
    refresh_catalog(tmp_path, fetch=_fake_fetch(payload))
    data = json.loads((tmp_path / "mcp_catalog_cache.json").read_text(encoding="utf-8"))
    assert data["servers"][0]["id"] == "srv-1"
