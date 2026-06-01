"""HTTP endpoint tests for the P12 entry browse / forget / backlinks routes.

Spawns a real ``WebSocketChannel`` on a test port, mints a bootstrap
token, and exercises the three new ``/api/memory/{entry,forget,backlinks}``
routes through HTTP. Smoke-level — the deep behaviour is covered by
``test_graph_api_entries.py`` (calling the helpers directly).
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from durin.channels.websocket import WebSocketChannel


def _seed_entry(
    ws: Path,
    *,
    class_name: str,
    entry_id: str,
    body: str = "obs",
    entities: tuple[str, ...] = ("person:alice",),
) -> Path:
    ent_lines = (
        "entities:\n" + "".join(f"  - {e}\n" for e in entities)
        if entities else ""
    )
    fm = (
        f"id: {entry_id}\n"
        f"headline: {entry_id} headline\n"
        f"valid_from: 2026-05-30\n"
        f"{ent_lines}"
    )
    p = ws / "memory" / class_name / f"{entry_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}---\n\n{body}\n", encoding="utf-8")
    return p


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


async def _get(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return await asyncio.to_thread(
        functools.partial(httpx.get, url, headers=headers or {}, timeout=5.0),
    )


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


@pytest.fixture()
def patched_workspace(tmp_path: Path):
    """Patch load_config to return a workspace pointing at tmp_path,
    with memory disabled (so the vector-cleanup branch in
    forget_entry is a no-op without needing fastembed)."""
    fake_cfg = SimpleNamespace(
        workspace_path=tmp_path,
        memory=SimpleNamespace(
            enabled=False,
            embedding=SimpleNamespace(model=""),
        ),
    )
    with patch("durin.config.loader.load_config", return_value=fake_cfg):
        yield tmp_path


@pytest.mark.asyncio
async def test_memory_entry_routes_require_bearer(
    bus: MagicMock, patched_workspace: Path,
) -> None:
    """All 3 new routes return 401 without a valid token."""
    channel = _ch(bus, port=29950)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        for path in (
            "/api/memory/entry?uri=memory/episodic/x",
            "/api/memory/forget?uri=memory/episodic/x",
            "/api/memory/backlinks?uri=memory/episodic/x",
        ):
            r = await _get(f"http://127.0.0.1:29950{path}")
            assert r.status_code == 401, f"expected 401 for {path}, got {r.status_code}"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_memory_entry_endpoint_returns_payload(
    bus: MagicMock, patched_workspace: Path,
) -> None:
    _seed_entry(
        patched_workspace, class_name="episodic", entry_id="obs-1",
        body="Alice loves rust",
    )
    channel = _ch(bus, port=29951)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _get("http://127.0.0.1:29951/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        r = await _get(
            "http://127.0.0.1:29951/api/memory/entry?uri=memory/episodic/obs-1",
            headers=auth,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["uri"] == "memory/episodic/obs-1"
        assert body["class_name"] == "episodic"
        assert "Alice loves rust" in body["body"]

        # 404 when the entry doesn't exist.
        r404 = await _get(
            "http://127.0.0.1:29951/api/memory/entry?uri=memory/episodic/ghost",
            headers=auth,
        )
        assert r404.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_memory_forget_endpoint_archives_and_protects(
    bus: MagicMock, patched_workspace: Path,
) -> None:
    _seed_entry(patched_workspace, class_name="episodic", entry_id="obs-2")
    channel = _ch(bus, port=29952)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _get("http://127.0.0.1:29952/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # Happy path: archive an existing entry.
        r = await _get(
            "http://127.0.0.1:29952/api/memory/forget?uri=memory/episodic/obs-2",
            headers=auth,
        )
        assert r.status_code == 200
        assert r.json() == {"result": "archived"}
        assert not (
            patched_workspace / "memory" / "episodic" / "obs-2.md"
        ).exists()
        assert (
            patched_workspace / "memory" / "archive" / "episodic" / "obs-2.md"
        ).exists()

        # Protected: entity URIs return 403.
        r_protected = await _get(
            "http://127.0.0.1:29952/api/memory/forget?uri=memory/entities/person/marcelo",
            headers=auth,
        )
        assert r_protected.status_code == 403
        assert r_protected.json()["result"] == "protected"

        # Invalid URI returns 400.
        r_bad = await _get(
            "http://127.0.0.1:29952/api/memory/forget?uri=garbage",
            headers=auth,
        )
        assert r_bad.status_code == 400
        assert r_bad.json()["result"] == "invalid"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_memory_backlinks_endpoint(
    bus: MagicMock, patched_workspace: Path,
) -> None:
    _seed_entry(patched_workspace, class_name="episodic", entry_id="target")
    _seed_entry(
        patched_workspace, class_name="episodic", entry_id="ref",
        body="see [[memory/episodic/target]] for more",
        entities=("person:bob",),
    )
    channel = _ch(bus, port=29953)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        boot = await _get("http://127.0.0.1:29953/webui/bootstrap")
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        r = await _get(
            "http://127.0.0.1:29953/api/memory/backlinks?uri=memory/episodic/target",
            headers=auth,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["uri"] == "memory/episodic/target"
        assert len(body["backlinks"]) == 1
        assert body["backlinks"][0]["uri"] == "memory/episodic/ref"
        assert body["truncated"] is False
    finally:
        await channel.stop()
        await server_task
