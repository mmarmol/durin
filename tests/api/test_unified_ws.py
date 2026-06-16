"""Step 3 of the ASGI unify: the Starlette WebSocket endpoint for the chat.

Tests verify:
- Connect to "/" → receive ``event: "ready"`` with a chat_id.
- Disconnect cleans up ``_subs`` (no leak).
- Rejection before accept when the channel requires a token but none is supplied
  (connect is refused / websocket closed before accept).
"""

import json

import pytest
from starlette.testclient import TestClient

from durin.bus.queue import MessageBus


@pytest.fixture()
def channel_and_client(tmp_path, monkeypatch):
    """Build a real WebSocketChannel + the unified app, no LLM."""
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
    app = build_gateway_http_app(channel, registry, auth=auth)
    client = TestClient(app)
    return channel, client


def test_ws_connect_receives_ready(channel_and_client):
    """Connecting to '/' must immediately deliver an ``event: "ready"`` frame
    with a non-empty ``chat_id``."""
    _, client = channel_and_client
    with client.websocket_connect("/") as ws:
        frame = json.loads(ws.receive_text())
        assert frame["event"] == "ready"
        assert frame["chat_id"]
        assert frame["client_id"]


def test_ws_disconnect_cleans_subs(channel_and_client):
    """After the connection closes, ``_subs`` must not retain the chat_id."""
    channel, client = channel_and_client
    chat_id = None
    with client.websocket_connect("/") as ws:
        frame = json.loads(ws.receive_text())
        chat_id = frame["chat_id"]
        # While connected, the chat_id is subscribed.
        assert chat_id in channel._subs

    # After the context manager exits (disconnect), _subs must be clean.
    assert chat_id not in channel._subs


def test_ws_auth_rejected_without_token(tmp_path, monkeypatch):
    """When ``websocketRequiresToken: True`` and no token is supplied,
    the server must close the connection before (or at) accept."""
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
        "websocketRequiresToken": True,
    }
    channel = WebSocketChannel(cfg, MessageBus())
    registry = channel._services
    auth = registry.get("auth")
    app = build_gateway_http_app(channel, registry, auth=auth)
    client = TestClient(app, raise_server_exceptions=False)

    # The server closes with code 1008 before accept; TestClient raises on connect.
    with pytest.raises(Exception):
        with client.websocket_connect("/") as ws:
            ws.receive_text()


def test_ws_auth_accepted_with_valid_token(tmp_path, monkeypatch):
    """When ``websocketRequiresToken: True`` and a valid issued token is supplied,
    the connection proceeds and delivers ``event: "ready"``."""
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
        "websocketRequiresToken": True,
    }
    channel = WebSocketChannel(cfg, MessageBus())
    registry = channel._services
    auth = registry.get("auth")
    app = build_gateway_http_app(channel, registry, auth=auth)
    client = TestClient(app)

    # Mint a token via bootstrap.
    r = client.get("/webui/bootstrap")
    assert r.status_code == 200
    token = r.json()["token"]

    with client.websocket_connect(f"/?token={token}") as ws:
        frame = json.loads(ws.receive_text())
        assert frame["event"] == "ready"
