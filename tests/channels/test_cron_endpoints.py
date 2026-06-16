"""HTTP endpoint characterization tests for the cron routes.

Spawns a real ``WebSocketChannel`` on test ports, mints a bootstrap token,
and exercises the four cron routes through HTTP.  These pin the exact JSON
shape and status codes that the shim must continue to produce after SP1.

Ports 29930-29934 — not used by any other test file.
"""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from durin.channels.websocket import WebSocketChannel


def _ch(bus: Any, port: int) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": port,
        "path": "/",
        "websocketRequiresToken": False,
    }
    return WebSocketChannel(cfg, bus, session_manager=None)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


async def _get(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return await asyncio.to_thread(
        functools.partial(httpx.get, url, headers=headers or {}, timeout=5.0)
    )


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
def patched_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Patch load_config so cron handlers resolve jobs.json inside tmp_path."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_jobs(ws)
    fake_cfg = SimpleNamespace(workspace_path=ws)
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: fake_cfg)
    return ws


@pytest.mark.asyncio
async def test_cron_routes_require_token(bus: MagicMock, patched_workspace: Path) -> None:
    """All four cron routes return 401 without a valid bearer token."""
    channel = _ch(bus, port=29930)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        for path in (
            "/api/cron",
            "/api/cron/remove?id=abc12345",
            "/api/cron/toggle?id=abc12345&enabled=false",
            "/api/cron/run?id=abc12345",
        ):
            r = await _get(f"http://127.0.0.1:29930{path}")
            assert r.status_code == 401, f"expected 401 for {path}, got {r.status_code}"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_cron_list_returns_jobs(bus: MagicMock, patched_workspace: Path) -> None:
    """`GET /api/cron` returns {jobs: [...]} with the expected shape."""
    channel = _ch(bus, port=29931)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _get("http://127.0.0.1:29931/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        r = await _get("http://127.0.0.1:29931/api/cron", headers=auth)
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
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_cron_remove_removes_user_job(bus: MagicMock, patched_workspace: Path) -> None:
    """`GET /api/cron/remove?id=` removes a user job and returns {result: removed}."""
    channel = _ch(bus, port=29932)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _get("http://127.0.0.1:29932/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}

        r = await _get("http://127.0.0.1:29932/api/cron/remove?id=abc12345", headers=auth)
        assert r.status_code == 200
        assert r.json() == {"result": "removed"}

        r404 = await _get("http://127.0.0.1:29932/api/cron/remove?id=ghost", headers=auth)
        assert r404.status_code == 404

        r403 = await _get("http://127.0.0.1:29932/api/cron/remove?id=sys00001", headers=auth)
        assert r403.status_code == 403

        r400 = await _get("http://127.0.0.1:29932/api/cron/remove", headers=auth)
        assert r400.status_code == 400
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_cron_toggle_enables_and_disables(bus: MagicMock, patched_workspace: Path) -> None:
    """`GET /api/cron/toggle?id=&enabled=` returns {job: {...}}."""
    channel = _ch(bus, port=29933)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _get("http://127.0.0.1:29933/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}

        r = await _get(
            "http://127.0.0.1:29933/api/cron/toggle?id=abc12345&enabled=false",
            headers=auth,
        )
        assert r.status_code == 200
        body = r.json()
        assert "job" in body
        assert body["job"]["id"] == "abc12345"
        assert body["job"]["enabled"] is False

        r404 = await _get(
            "http://127.0.0.1:29933/api/cron/toggle?id=ghost&enabled=true",
            headers=auth,
        )
        assert r404.status_code == 404

        r400 = await _get(
            "http://127.0.0.1:29933/api/cron/toggle?enabled=true",
            headers=auth,
        )
        assert r400.status_code == 400
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_cron_run_returns_503_when_scheduler_unavailable(
    bus: MagicMock, patched_workspace: Path
) -> None:
    """`GET /api/cron/run` returns 503 when no live scheduler is present."""
    channel = _ch(bus, port=29934)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _get("http://127.0.0.1:29934/webui/bootstrap")
        auth = {"Authorization": f"Bearer {boot.json()['token']}"}

        r = await _get("http://127.0.0.1:29934/api/cron/run?id=abc12345", headers=auth)
        assert r.status_code == 503
    finally:
        await channel.stop()
        await server_task
