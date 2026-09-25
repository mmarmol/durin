"""mcp_manage must get a live McpRuntime through ToolContext, the same way
the gateway's REST service registry gets one (cli/commands.py's unified
wiring: ``McpRuntime(agent)``) — not by injecting a runtime into McpService
by hand, which is how every other mcp_manage test in this suite builds the
tool.

Before this fix, ``ToolContext`` had no ``mcp_runtime`` field at all, so
``AgentLoop._register_default_tools`` always built ``McpManageTool`` over a
config-only ``McpService`` (``_runtime is None``). ``McpService.approved_
config`` then always returned ``None``, and ``mcp_manage``'s reconnect gate
refuses whenever there is no approved record — which made it refuse to
reconnect even a server whose config had never changed since boot, and left
an approved add/update/enable unable to ever record or connect anything.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.config.loader import get_config_path, load_config, save_config
from durin.config.schema import Config, MCPServerConfig


class _FakeConn:
    _registered_names: list = []
    _error = None

    def breaker_state(self):
        return SimpleNamespace(value="closed")

    async def aclose(self) -> None:
        pass


def _make_loop(tmp_path, monkeypatch, mcp_servers: dict) -> AgentLoop:
    async def fake_connect(servers, registry, **kwargs):
        return {name: _FakeConn() for name in servers}

    monkeypatch.setattr("durin.agent.tools.mcp.connect_mcp_servers", fake_connect)
    provider = MagicMock()
    provider.get_default_model.return_value = "m"
    # No app_config passed, same as the real gateway's construction path and
    # probe_r2_wiring.py — McpManageTool.create() then falls back to
    # load_config() for install_policy, reading whatever was saved to disk.
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     model="m", mcp_servers=mcp_servers)


@pytest.mark.asyncio
async def test_mcp_manage_tool_context_carries_the_loops_live_mcp_runtime(
    tmp_path, monkeypatch,
) -> None:
    cfg = Config()
    cfg.tools.mcp_servers["s"] = MCPServerConfig(command="echo")
    save_config(cfg, get_config_path())

    loop = _make_loop(tmp_path, monkeypatch, load_config().tools.mcp_servers)
    tool = loop.tools.get("mcp_manage")

    assert tool is not None
    assert tool._service._runtime is not None


@pytest.mark.asyncio
async def test_agent_reconnect_of_an_unchanged_boot_config_succeeds(
    tmp_path, monkeypatch,
) -> None:
    """Mirrors probe_r2_wiring.py: a server whose config is exactly what it
    was at boot must reconnect through the loop-registered tool — the
    boot-time snapshot IS an approved record, with zero extra wiring."""
    cfg = Config()
    cfg.tools.mcp_servers["s"] = MCPServerConfig(command="echo")
    save_config(cfg, get_config_path())

    loop = _make_loop(tmp_path, monkeypatch, load_config().tools.mcp_servers)
    tool = loop.tools.get("mcp_manage")

    out = await tool.execute(action="reconnect", name="s")
    assert "error" not in out


@pytest.mark.asyncio
async def test_agent_reconnect_succeeds_after_its_own_approved_update(
    tmp_path, monkeypatch,
) -> None:
    """install_policy=auto: the update runs with no approval prompt, and the
    agent's own later reconnect must succeed — it must not refuse just
    because reconnect itself never connected before."""
    cfg = Config()
    cfg.tools.mcp_servers["good"] = MCPServerConfig(url="https://good/mcp")
    cfg.tools.mcp_discovery.install_policy = "auto"
    save_config(cfg, get_config_path())

    loop = _make_loop(tmp_path, monkeypatch, load_config().tools.mcp_servers)
    tool = loop.tools.get("mcp_manage")

    r1 = await tool.execute(action="update", name="good",
                            config={"url": "https://good/mcp", "tool_timeout": 99})
    assert "error" not in r1

    r2 = await tool.execute(action="reconnect", name="good")
    assert "error" not in r2
