import asyncio
import types

import durin.agent.loop as loop_mod


def test_connect_mcp_installs_then_retries(monkeypatch):
    """A missing `mcp` extra surfaces as ImportError inside connect_mcp_servers;
    ensure_or_note installs it and the connect retries in-process (lazy import)."""
    calls = {"ensure": 0, "connect": 0}
    seq = iter([ImportError("no mcp"), []])

    async def fake_connect(servers, tools, **kwargs):
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
    # app_config=None makes the site load a config for the auto-install gate;
    # keep the test off the real loader even though ensure_or_note is mocked.
    monkeypatch.setattr(
        "durin.config.loader.load_config", lambda *a, **k: types.SimpleNamespace()
    )

    lp = loop_mod.AgentLoop.__new__(loop_mod.AgentLoop)
    lp._mcp_connected = False
    lp._mcp_connecting = False
    lp._mcp_servers = {"x": object()}  # _mcp_servers is always a dict[name, cfg]
    lp._mcp_connections = {}
    lp._mcp_connect_errors = {}
    lp.tools = []
    lp.app_config = None
    lp.provider = None
    lp.model = None
    lp.workspace = None
    asyncio.run(lp._connect_mcp())
    assert calls["ensure"] == 1
    assert calls["connect"] == 1


def test_connect_mcp_opted_out_config_blocks_install(monkeypatch):
    """Most AgentLoops run with app_config=None, so the site loads a config
    for the gate: install.auto_install_extras=False must block the install
    (no installer subprocess) and skip the post-install connect retry."""
    import durin.extras as ex

    calls = {"connect": 0}

    async def fake_connect(servers, tools, **kwargs):
        calls["connect"] += 1
        raise ImportError("no mcp")

    monkeypatch.setattr(
        "durin.agent.tools.mcp.connect_mcp_servers", fake_connect, raising=False
    )
    opted_out = types.SimpleNamespace(
        install=types.SimpleNamespace(auto_install_extras=False)
    )
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: opted_out)
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    monkeypatch.setattr(ex, "_extra_specs", lambda extra: ["mcp"])
    monkeypatch.setattr(ex, "_installer_cmd", lambda specs: ["echo", *specs])
    run_calls = []
    monkeypatch.setattr(
        ex.subprocess, "run",
        lambda *a, **k: run_calls.append(a)
        or types.SimpleNamespace(returncode=0, stderr="", stdout=""),
    )

    lp = loop_mod.AgentLoop.__new__(loop_mod.AgentLoop)
    lp._mcp_connected = False
    lp._mcp_connecting = False
    lp._mcp_servers = {"x": object()}
    lp._mcp_connections = {}
    lp._mcp_connect_errors = {}
    lp.tools = []
    lp.app_config = None
    lp.provider = None
    lp.model = None
    lp.workspace = None
    asyncio.run(lp._connect_mcp())
    assert run_calls == []  # opt-out honored: no installer subprocess
    assert calls["connect"] == 1  # no post-install retry
    assert lp._mcp_connected is False
