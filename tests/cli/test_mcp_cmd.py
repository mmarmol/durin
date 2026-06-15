"""Tests for SP-4c: LoopbackCallback + durin mcp login/logout/status CLI.

These are pure unit tests — no real browser, no real OAuth network.
"""
from __future__ import annotations

import asyncio
import urllib.request

import pytest

import durin.security.secrets as _secrets


def _point_store_at(tmp_path, monkeypatch):
    """Redirect the secret store to a throwaway path."""
    secrets_file = tmp_path / "secrets.json"
    monkeypatch.setattr("durin.security.secrets._default_secrets_path", lambda: secrets_file)
    _secrets._STORE = None
    return secrets_file


def _config_with_servers(monkeypatch, servers: dict):
    """Monkeypatch load_config to return a config with the given mcp_servers dict."""
    from durin.config.schema import Config, MCPServerConfig, ToolsConfig

    parsed: dict[str, MCPServerConfig] = {}
    for name, raw in servers.items():
        parsed[name] = MCPServerConfig.model_validate(raw)

    tools = ToolsConfig(mcp_servers=parsed)
    config = Config(tools=tools)

    monkeypatch.setattr("durin.cli.mcp_cmd.load_config", lambda: config)


# ---------------------------------------------------------------------------
# 4c.1 — LoopbackCallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loopback_callback_captures_code_and_state():
    from durin.agent.tools.mcp_oauth import LoopbackCallback

    cb = LoopbackCallback(port=0)  # port=0 → OS assigns a free port
    state = cb.state
    cb.start()
    try:
        url = f"http://127.0.0.1:{cb.port}/callback?code=the-code&state={state}"
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: urllib.request.urlopen(url, timeout=5).read()
        )
        code, returned_state = await asyncio.wait_for(cb.wait(), timeout=5)
        assert code == "the-code"
        assert returned_state == state
    finally:
        cb.stop()


@pytest.mark.asyncio
async def test_loopback_callback_rejects_bad_state():
    from durin.agent.tools.mcp_oauth import LoopbackCallback

    cb = LoopbackCallback(port=0)
    cb.start()
    try:
        url = f"http://127.0.0.1:{cb.port}/callback?code=c&state=WRONG"
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: urllib.request.urlopen(url, timeout=5).read()
        )
        # Future must NOT be resolved — bad state is rejected.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(cb.wait(), timeout=1)
    finally:
        cb.stop()


@pytest.mark.asyncio
async def test_loopback_callback_missing_code_rejected():
    from durin.agent.tools.mcp_oauth import LoopbackCallback

    cb = LoopbackCallback(port=0)
    cb.start()
    try:
        url = f"http://127.0.0.1:{cb.port}/callback?state={cb.state}"
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: urllib.request.urlopen(url, timeout=5).read()
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(cb.wait(), timeout=1)
    finally:
        cb.stop()


@pytest.mark.asyncio
async def test_loopback_callback_success_html():
    from durin.agent.tools.mcp_oauth import LoopbackCallback

    cb = LoopbackCallback(port=0)
    cb.start()
    try:
        url = f"http://127.0.0.1:{cb.port}/callback?code=c&state={cb.state}"
        html = await asyncio.get_event_loop().run_in_executor(
            None, lambda: urllib.request.urlopen(url, timeout=5).read().decode()
        )
        assert "durin" in html.lower() or "close" in html.lower()
    finally:
        cb.stop()


# ---------------------------------------------------------------------------
# 4c.2 — CLI: login / logout / status
# ---------------------------------------------------------------------------


def test_login_unknown_server(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from durin.cli.mcp_cmd import mcp_app

    _config_with_servers(monkeypatch, {})
    result = CliRunner().invoke(mcp_app, ["login", "nope"])
    assert result.exit_code != 0
    out = result.output.lower()
    assert "not configured" in out or "unknown" in out or "nope" in out


def test_login_non_oauth_server(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from durin.cli.mcp_cmd import mcp_app

    _config_with_servers(monkeypatch, {"plain": {"url": "https://x/mcp"}})
    result = CliRunner().invoke(mcp_app, ["login", "plain"])
    assert result.exit_code != 0
    assert "oauth" in result.output.lower()


def test_login_runs_flow_and_persists(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from durin.cli.mcp_cmd import mcp_app

    _point_store_at(tmp_path, monkeypatch)
    _config_with_servers(
        monkeypatch, {"acme": {"url": "https://api.example/mcp", "oauth": True}}
    )

    async def _fake_run_login(server, cfg):
        from mcp.shared.auth import OAuthToken

        from durin.agent.tools.mcp_oauth import SecretsTokenStorage

        st = SecretsTokenStorage(server, server_url=cfg.url or None)
        await st.set_tokens(OAuthToken(access_token="logged-in"))

    monkeypatch.setattr("durin.cli.mcp_cmd._run_login_flow", _fake_run_login)

    result = CliRunner().invoke(mcp_app, ["login", "acme"])
    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "signed in" in out or "success" in out or "acme" in out

    from durin.agent.tools.mcp_oauth import SecretsTokenStorage

    st = SecretsTokenStorage("acme", server_url="https://api.example/mcp")
    assert asyncio.run(st.get_tokens()).access_token == "logged-in"


def test_login_failure_exits_nonzero(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from durin.cli.mcp_cmd import mcp_app

    _point_store_at(tmp_path, monkeypatch)
    _config_with_servers(
        monkeypatch, {"acme": {"url": "https://api.example/mcp", "oauth": True}}
    )

    async def _broken(server, cfg):
        raise RuntimeError("network down")

    monkeypatch.setattr("durin.cli.mcp_cmd._run_login_flow", _broken)

    result = CliRunner().invoke(mcp_app, ["login", "acme"])
    assert result.exit_code != 0


def test_status_reports_presence(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from durin.cli.mcp_cmd import mcp_app

    _point_store_at(tmp_path, monkeypatch)
    _config_with_servers(
        monkeypatch, {"acme": {"url": "https://api.example/mcp", "oauth": True}}
    )
    from mcp.shared.auth import OAuthToken

    from durin.agent.tools.mcp_oauth import SecretsTokenStorage

    asyncio.run(
        SecretsTokenStorage("acme", server_url="https://api.example/mcp").set_tokens(
            OAuthToken(access_token="t")
        )
    )
    result = CliRunner().invoke(mcp_app, ["status"])
    assert result.exit_code == 0
    assert "acme" in result.output
    out = result.output.lower()
    assert "signed in" in out or "✓" in out


def test_status_no_oauth_servers(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from durin.cli.mcp_cmd import mcp_app

    _point_store_at(tmp_path, monkeypatch)
    _config_with_servers(monkeypatch, {"plain": {"url": "https://x/mcp"}})
    result = CliRunner().invoke(mcp_app, ["status"])
    assert result.exit_code == 0
    assert "no oauth" in result.output.lower() or "none" in result.output.lower() or "plain" not in result.output


def test_logout_forgets(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from durin.cli.mcp_cmd import mcp_app

    _point_store_at(tmp_path, monkeypatch)
    _config_with_servers(
        monkeypatch, {"acme": {"url": "https://api.example/mcp", "oauth": True}}
    )
    from mcp.shared.auth import OAuthToken

    from durin.agent.tools.mcp_oauth import SecretsTokenStorage

    asyncio.run(
        SecretsTokenStorage("acme", server_url="https://api.example/mcp").set_tokens(
            OAuthToken(access_token="t")
        )
    )
    result = CliRunner().invoke(mcp_app, ["logout", "acme"])
    assert result.exit_code == 0

    st = SecretsTokenStorage("acme", server_url="https://api.example/mcp")
    assert asyncio.run(st.get_tokens()) is None


def test_logout_no_tokens_is_graceful(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from durin.cli.mcp_cmd import mcp_app

    _point_store_at(tmp_path, monkeypatch)
    _config_with_servers(
        monkeypatch, {"acme": {"url": "https://api.example/mcp", "oauth": True}}
    )
    result = CliRunner().invoke(mcp_app, ["logout", "acme"])
    assert result.exit_code == 0  # graceful, not an error
