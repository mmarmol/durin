"""Tests for MCP runtime surface: enabled-aware startup, per-server
connect/disconnect, and the McpRuntime accessor."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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


async def test_disconnect_mcp_server_unknown_is_noop(tmp_path):
    # Idempotent: disconnecting a server with no live connection must NOT raise — a server
    # installed straight to needs_auth (never connected) isn't in the runtime snapshot, and
    # reconnect()/post-OAuth reconnect do disconnect-then-connect on it. Raising here left
    # such servers stuck "connecting" (the connect never ran).
    loop = _loop(tmp_path, {})
    await loop.disconnect_mcp_server("nope")  # no raise
    assert "nope" not in loop._mcp_connections


# --- McpRuntime accessor --------------------------------------------------


def test_mcp_runtime_live_status_reports_per_connection():
    from durin.agent.mcp_runtime import McpRuntime
    from durin.agent.tools.mcp_connection import BreakerState

    registry = MagicMock()
    registry.get.side_effect = lambda n: (
        SimpleNamespace(description="tool A") if n == "mcp_x_a" else None
    )

    conn_ok = MagicMock()
    conn_ok.breaker_state.return_value = BreakerState.CLOSED
    conn_ok._error = None
    conn_ok._registered_names = ["mcp_x_a"]

    conn_bad = MagicMock()
    conn_bad.breaker_state.return_value = BreakerState.OPEN
    conn_bad._error = RuntimeError("boom")
    conn_bad._registered_names = []

    loop = SimpleNamespace(
        tools=registry, _mcp_connections={"x": conn_ok, "y": conn_bad}
    )
    status = McpRuntime(loop).live_status()

    assert status["x"].breaker_state == "closed"
    assert status["x"].error is None
    assert status["x"].tools == [("mcp_x_a", "tool A")]
    assert status["y"].breaker_state == "open"
    assert status["y"].error == "boom"
    assert status["y"].tools == []


async def test_mcp_runtime_connect_disconnect_delegate():
    from durin.agent.mcp_runtime import McpRuntime

    loop = MagicMock()
    loop.connect_mcp_server = AsyncMock()
    loop.disconnect_mcp_server = AsyncMock()
    rt = McpRuntime(loop)

    await rt.connect("x")
    await rt.disconnect("x")

    loop.connect_mcp_server.assert_awaited_once_with("x", None)
    loop.disconnect_mcp_server.assert_awaited_once_with("x")


# --- connect-error tracking (failed servers, opencode parity) -------------


async def test_failed_connect_records_error_and_runtime_exposes_it(tmp_path):
    from durin.agent.mcp_runtime import McpRuntime

    cfg = MCPServerConfig(command="durin-nonexistent-cmd-xyz", enabled=True)
    loop = _loop(tmp_path, {"bad": cfg})
    rt = McpRuntime(loop)

    await loop.connect_mcp_server("bad", cfg)  # spawn fails

    assert "bad" not in loop._mcp_connections  # never connected
    assert "bad" in loop._mcp_connect_errors  # failure recorded
    assert "bad" in rt.connect_errors()
    assert rt.connect_errors()["bad"]  # a non-empty message

    # An intentional disconnect clears the failure record.
    await loop.disconnect_mcp_server("bad")
    assert "bad" not in rt.connect_errors()
