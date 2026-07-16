"""The public MCP OAuth callback route: provider redirects arrive with no
durin session, so the route sits with the other auth-exempt routes (next to
``/webui/bootstrap``) and is gated solely by the single-use state.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from durin.bus.queue import MessageBus


def _build_app(tmp_path, monkeypatch):
    # Isolate the persisted token store to a tmp dir (same pattern as
    # test_unified_http.py's _build_app — the route lives on the same app).
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    from durin.api.asgi import build_gateway_http_app
    from durin.channels.websocket import WebSocketChannel

    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    channel = WebSocketChannel(cfg, MessageBus())
    registry = channel._services
    auth = registry.get("auth")
    return build_gateway_http_app(channel, registry, auth=auth)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    app = _build_app(tmp_path, monkeypatch)
    return TestClient(app)


def _make_callback():
    # GatewayCallback() creates an asyncio.Future, which needs a running loop.
    from durin.agent.tools.mcp_oauth_web import GatewayCallback

    async def _register():
        cb = GatewayCallback()
        cb.start()
        return cb

    return asyncio.run(_register())


def test_callback_unknown_state_is_400(client):
    r = client.get("/api/v1/mcp/oauth/callback?state=nope&code=x")
    assert r.status_code == 400


def test_callback_missing_params_is_400(client):
    assert client.get("/api/v1/mcp/oauth/callback").status_code == 400
    assert client.get("/api/v1/mcp/oauth/callback?state=s").status_code == 400


def test_callback_valid_state_returns_close_page_and_is_single_use(client):
    cb = _make_callback()
    r = client.get(f"/api/v1/mcp/oauth/callback?state={cb.state}&code=c0de")
    assert r.status_code == 200
    assert "close this window" in r.text
    # Reuse rejected.
    r2 = client.get(f"/api/v1/mcp/oauth/callback?state={cb.state}&code=c0de")
    assert r2.status_code == 400


def test_callback_provider_error_renders_failure_page_and_consumes_state(client):
    cb = _make_callback()
    r = client.get(f"/api/v1/mcp/oauth/callback?state={cb.state}&error=access_denied")
    assert r.status_code == 200
    assert "Sign-in failed" in r.text
    assert "Signed in" not in r.text
    assert "access_denied" in r.text
    # Reuse rejected — the error still consumed the single-use state.
    r2 = client.get(f"/api/v1/mcp/oauth/callback?state={cb.state}&error=access_denied")
    assert r2.status_code == 400
