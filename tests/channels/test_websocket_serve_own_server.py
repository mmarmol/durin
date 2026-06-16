"""Step 4 of the ASGI unify: _serve_own_server flag on WebSocketChannel.

Tests verify:
- Default value is True (old behaviour unchanged unless gateway opts in).
- Setting to False before start() makes start() return immediately without
  blocking on a real socket.  The channel is still usable as a handler
  (_running=True, _stop_event set, _subs empty but addressable).
- stop() is safe when _serve_own_server was False (no _server_task was
  created — the guard in stop() must not raise).
"""

import asyncio

import pytest

from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel


@pytest.fixture()
def channel(tmp_path, monkeypatch):
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    return WebSocketChannel(cfg, MessageBus())


def test_default_serve_own_server_is_true(channel):
    """Fresh channel always defaults to True (legacy behaviour)."""
    assert channel._serve_own_server is True


def test_flag_false_makes_start_noop(channel):
    """start() with _serve_own_server=False must complete without opening a socket."""
    channel._serve_own_server = False

    async def run():
        await channel.start()
        # Channel reports running but no server task was created.
        assert channel._running is True
        assert channel._stop_event is not None
        assert channel._server_task is None

    asyncio.run(run())


def test_stop_safe_after_noop_start(channel):
    """stop() must not raise when _server_task was never created."""
    channel._serve_own_server = False

    async def run():
        await channel.start()
        await channel.stop()  # must not raise
        assert channel._running is False

    asyncio.run(run())


def test_unified_app_serves_ws_and_api(tmp_path, monkeypatch):
    """build_gateway_http_app over a channel with _serve_own_server=False
    still exposes the WS route (/) and /api/v1/health."""
    import json

    from starlette.testclient import TestClient

    from durin.api.asgi import build_gateway_http_app

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    channel = WebSocketChannel(cfg, MessageBus())
    # Simulate what the gateway does in unified-server mode.
    channel._serve_own_server = False
    registry = channel._services
    auth = registry.get("auth")

    app = build_gateway_http_app(channel, registry, auth=auth)
    client = TestClient(app)

    # /api/v1/health must be reachable (no auth required).
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

    # WS connect must deliver event: "ready".
    with client.websocket_connect("/") as ws:
        frame = json.loads(ws.receive_text())
        assert frame["event"] == "ready"
        assert frame["chat_id"]
