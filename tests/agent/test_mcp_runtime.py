"""Tests for MCP runtime surface: enabled-aware startup, per-server
connect/disconnect, and the McpRuntime accessor."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.config.schema import MCPServerConfig


def _loop(tmp_path: Path, mcp_servers: dict) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        mcp_servers=mcp_servers,
    )


async def test_connect_mcp_skips_disabled_servers(tmp_path, monkeypatch):
    captured: dict = {}

    async def fake_connect(mcp_servers, registry, **kwargs):
        captured["servers"] = dict(mcp_servers)
        return {}

    monkeypatch.setattr("durin.agent.tools.mcp.connect_mcp_servers", fake_connect)

    loop = _loop(
        tmp_path,
        {
            "on": MCPServerConfig(url="https://a/mcp", enabled=True),
            "off": MCPServerConfig(url="https://b/mcp", enabled=False),
        },
    )
    await loop._connect_mcp()

    assert "on" in captured["servers"]
    assert "off" not in captured["servers"]
    # The full configured set is preserved so a disabled server can be
    # re-enabled (connected) at runtime later.
    assert set(loop._mcp_servers) == {"on", "off"}


# --- per-server connect/disconnect ---------------------------------------


async def test_connect_mcp_server_adds_one_connection(tmp_path, monkeypatch):
    fake_conn = object()

    async def fake_connect(mcp_servers, registry, **kwargs):
        assert set(mcp_servers) == {"x"}  # only the requested server
        return {"x": fake_conn}

    monkeypatch.setattr("durin.agent.tools.mcp.connect_mcp_servers", fake_connect)

    loop = _loop(tmp_path, {"x": MCPServerConfig(url="https://x/mcp", enabled=False)})
    assert "x" not in loop._mcp_connections

    await loop.connect_mcp_server("x")

    assert loop._mcp_connections["x"] is fake_conn


async def test_connect_mcp_server_is_idempotent(tmp_path, monkeypatch):
    calls = {"n": 0}

    async def fake_connect(mcp_servers, registry, **kwargs):
        calls["n"] += 1
        return {"x": object()}

    monkeypatch.setattr("durin.agent.tools.mcp.connect_mcp_servers", fake_connect)

    loop = _loop(tmp_path, {"x": MCPServerConfig(url="https://x/mcp")})
    await loop.connect_mcp_server("x")
    await loop.connect_mcp_server("x")  # already connected → no second connect

    assert calls["n"] == 1


async def test_connect_mcp_server_unknown_raises(tmp_path):
    loop = _loop(tmp_path, {})
    with pytest.raises(KeyError):
        await loop.connect_mcp_server("nope")


async def test_disconnect_mcp_server_closes_and_removes(tmp_path):
    loop = _loop(tmp_path, {"x": MCPServerConfig(url="https://x/mcp")})
    conn = MagicMock()
    conn.aclose = AsyncMock()
    loop._mcp_connections["x"] = conn

    await loop.disconnect_mcp_server("x")

    conn.aclose.assert_awaited_once()
    assert "x" not in loop._mcp_connections


async def test_disconnect_mcp_server_not_connected_is_noop(tmp_path):
    loop = _loop(tmp_path, {"x": MCPServerConfig(url="https://x/mcp")})
    await loop.disconnect_mcp_server("x")  # configured but not connected
    assert "x" not in loop._mcp_connections


async def test_disconnect_mcp_server_unknown_raises(tmp_path):
    loop = _loop(tmp_path, {})
    with pytest.raises(KeyError):
        await loop.disconnect_mcp_server("nope")
