import asyncio
import types

import durin.agent.loop as loop_mod


def test_connect_mcp_installs_then_retries(monkeypatch):
    """A missing `mcp` extra surfaces as ImportError inside connect_mcp_servers;
    ensure_or_note installs it and the connect retries in-process (lazy import)."""
    calls = {"ensure": 0, "connect": 0}
    seq = iter([ImportError("no mcp"), []])

    async def fake_connect(servers, tools):
        v = next(seq)
        if isinstance(v, ImportError):
            raise v
        calls["connect"] += 1
        return v

    monkeypatch.setattr(
        "durin.agent.tools.mcp.connect_mcp_servers", fake_connect, raising=False
    )

    def fake_ensure(feature, *, config):
        calls["ensure"] += 1
        assert feature == "mcp"
        return types.SimpleNamespace(status="installed", needs_restart=False, message="")

    monkeypatch.setattr(loop_mod, "ensure_or_note", fake_ensure)

    lp = loop_mod.AgentLoop.__new__(loop_mod.AgentLoop)
    lp._mcp_connected = False
    lp._mcp_connecting = False
    lp._mcp_servers = [object()]
    lp._mcp_stacks = []
    lp.tools = []
    lp.app_config = None
    asyncio.run(lp._connect_mcp())
    assert calls["ensure"] == 1
    assert calls["connect"] == 1
