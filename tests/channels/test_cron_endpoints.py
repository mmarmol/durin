"""HTTP endpoint characterization tests for the cron routes.

Uses the unified Starlette ASGI app (``build_gateway_http_app``) via
``TestClient`` instead of spawning a real WebSocketChannel socket.  These pin
the exact JSON shape and status codes the cron routes must continue to produce.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_gateway_http_app
from durin.channels.websocket import WebSocketChannel


def _seed_jobs(ws: Path) -> None:
    """Write a minimal jobs.json with one user job and one system job."""
    cron_dir = ws / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    jobs_data = {
        "version": 1,
        "jobs": [
            {
                "id": "abc12345",
                "name": "test job",
                "enabled": True,
                "schedule": {"kind": "every", "everyMs": 3600000, "atMs": None, "expr": None, "tz": None},
                "payload": {"kind": "agent_turn", "message": "hello", "deliver": False, "channel": None, "to": None, "channelMeta": {}, "sessionKey": None},
                "state": {
                    "nextRunAtMs": None,
                    "lastRunAtMs": None,
                    "lastStatus": None,
                    "lastError": None,
                    "runHistory": [],
                },
                "createdAtMs": 1000000,
                "updatedAtMs": 1000000,
                "deleteAfterRun": False,
            },
            {
                "id": "sys00001",
                "name": "system consolidation",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "0 3 * * *", "tz": "UTC", "atMs": None, "everyMs": None},
                "payload": {"kind": "system_event", "message": "__consolidate__", "deliver": False, "channel": None, "to": None, "channelMeta": {}, "sessionKey": None},
                "state": {
                    "nextRunAtMs": None,
                    "lastRunAtMs": None,
                    "lastStatus": None,
                    "lastError": None,
                    "runHistory": [],
                },
                "createdAtMs": 1000000,
                "updatedAtMs": 1000000,
                "deleteAfterRun": False,
            },
        ],
    }
    (cron_dir / "jobs.json").write_text(json.dumps(jobs_data), encoding="utf-8")


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bus: MagicMock) -> TestClient:
    """Unified ASGI test client with isolated data dir and workspace."""
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_jobs(ws)
    fake_cfg = SimpleNamespace(workspace_path=ws)
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: fake_cfg)

    spa = tmp_path / "dist"
    spa.mkdir()
    (spa / "index.html").write_text(
        "<!doctype html><title>durin</title><div id=root></div>", encoding="utf-8"
    )

    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    channel = WebSocketChannel(cfg, bus)
    registry = channel._services
    auth = registry.get("auth")
    app = build_gateway_http_app(channel, registry, auth=auth, static_dist_path=spa)
    return TestClient(app)


def _token(client: TestClient) -> str:
    r = client.get("/webui/bootstrap")
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_cron_routes_require_token(client: TestClient) -> None:
    """All four cron routes return 401 without a valid bearer token."""
    for path in (
        "/api/cron",
        "/api/cron/remove?id=abc12345",
        "/api/cron/toggle?id=abc12345&enabled=false",
        "/api/cron/run?id=abc12345",
    ):
        r = client.get(path)
        assert r.status_code == 401, f"expected 401 for {path}, got {r.status_code}"


def test_cron_list_returns_jobs(client: TestClient) -> None:
    """`GET /api/cron` returns {jobs: [...]} with the expected shape."""
    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    r = client.get("/api/cron", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "jobs" in body
    jobs = body["jobs"]
    assert len(jobs) == 2

    user_job = next(j for j in jobs if j["id"] == "abc12345")
    assert user_job["name"] == "test job"
    assert user_job["enabled"] is True
    assert user_job["is_system"] is False
    assert user_job["message"] == "hello"
    assert user_job["schedule"]["kind"] == "every"
    assert user_job["schedule"]["label"] == "every 1h"
    assert "state" in user_job

    sys_job = next(j for j in jobs if j["id"] == "sys00001")
    assert sys_job["is_system"] is True
    assert sys_job["message"] == ""  # system jobs hide their message
    assert sys_job["schedule"]["kind"] == "cron"
    assert "0 3 * * *" in sys_job["schedule"]["label"]


def test_cron_remove_removes_user_job(client: TestClient) -> None:
    """`GET /api/cron/remove?id=` removes a user job and returns {result: removed}."""
    auth = {"Authorization": f"Bearer {_token(client)}"}

    r = client.get("/api/cron/remove?id=abc12345", headers=auth)
    assert r.status_code == 200
    assert r.json() == {"result": "removed"}

    r404 = client.get("/api/cron/remove?id=ghost", headers=auth)
    assert r404.status_code == 404

    r403 = client.get("/api/cron/remove?id=sys00001", headers=auth)
    assert r403.status_code == 403

    r400 = client.get("/api/cron/remove", headers=auth)
    assert r400.status_code == 400


def test_cron_toggle_enables_and_disables(client: TestClient) -> None:
    """`GET /api/cron/toggle?id=&enabled=` returns {job: {...}}."""
    auth = {"Authorization": f"Bearer {_token(client)}"}

    r = client.get("/api/cron/toggle?id=abc12345&enabled=false", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert "job" in body
    assert body["job"]["id"] == "abc12345"
    assert body["job"]["enabled"] is False

    r404 = client.get("/api/cron/toggle?id=ghost&enabled=true", headers=auth)
    assert r404.status_code == 404

    r400 = client.get("/api/cron/toggle?enabled=true", headers=auth)
    assert r400.status_code == 400


def test_cron_run_returns_503_when_scheduler_unavailable(client: TestClient) -> None:
    """`GET /api/cron/run` returns 503 when no live scheduler is present."""
    auth = {"Authorization": f"Bearer {_token(client)}"}

    r = client.get("/api/cron/run?id=abc12345", headers=auth)
    assert r.status_code == 503
