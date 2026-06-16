"""Tests for MCP runtime surface: enabled-aware startup, per-server
connect/disconnect, and the McpRuntime accessor."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

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
