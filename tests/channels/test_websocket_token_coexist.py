import time
from unittest.mock import AsyncMock, MagicMock

from durin.channels.websocket import WebSocketChannel


def _channel(**extra):
    cfg = {"enabled": True, "host": "127.0.0.1", "port": 0, "path": "/", **extra}
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    return WebSocketChannel(cfg, bus)


def test_static_token_accepts_static_value():
    ch = _channel(token="s3cr3t-static")
    assert ch._ws_auth_ok({"token": ["s3cr3t-static"]}) is True


def test_static_token_set_still_accepts_bootstrap_ephemeral():
    """A configured static token must NOT shut out the dashboard's
    short-lived issued tokens — they coexist (auth is an OR)."""
    ch = _channel(token="s3cr3t-static")
    ephemeral = "nbwt_ephemeral_example"
    ch._issued_tokens[ephemeral] = time.monotonic() + 300.0
    assert ch._ws_auth_ok({"token": [ephemeral]}) is True


def test_static_token_rejects_unknown_token():
    ch = _channel(token="s3cr3t-static")
    assert ch._ws_auth_ok({"token": ["wrong"]}) is False
