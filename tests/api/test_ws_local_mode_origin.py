"""In local mode, the socket takes an anonymous connection only from this machine.

With no setup secret and no token required (the gateway's default when the
config has no ``[channels.websocket]`` section), the socket accepts a
connection without a token. A page in the local browser could otherwise open
it and chat with the agent: through DNS rebinding (its own name resolving to
127.0.0.1, sent as ``Host``), or straight to ``ws://127.0.0.1`` from any site
(a socket is not bound by the same-origin policy; the page's site arrives as
``Origin``). So an anonymous handshake in local mode needs a loopback ``Host``
and, when an ``Origin`` is sent, a loopback ``Origin`` host. A non-browser local
client sends no ``Origin`` and still connects. The same rule applies whenever no
token is required, whether or not a setup secret is configured: a secret only
changes how a token is obtained, and an anonymous connection carries none. A
connection with a valid token, and a token-required config, are unchanged — a
page on another site cannot obtain a bootstrap token in the first place.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from durin.api.asgi import build_gateway_http_app
from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel


def _client(tmp_path, monkeypatch, *, base_url: str = "http://127.0.0.1:8765",
            **cfg_extra: Any) -> tuple[WebSocketChannel, TestClient]:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    cfg = {"enabled": True, "allowFrom": ["*"], "host": "127.0.0.1", "port": 8765,
           "path": "/", "websocketRequiresToken": False, **cfg_extra}
    channel = WebSocketChannel(cfg, MessageBus())
    registry = channel._services
    app = build_gateway_http_app(channel, registry, auth=registry.get("auth"),
                                 static_token=cfg_extra.get("token", ""))
    return channel, TestClient(app, base_url=base_url)


def _accepted(client: TestClient, path: str = "/", headers: dict | None = None) -> None:
    with client.websocket_connect(path, headers=headers or {}) as ws:
        assert ws.receive_json()["event"] == "ready"


def _refused(client: TestClient, path: str = "/", headers: dict | None = None) -> None:
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(path, headers=headers or {}) as ws:
            ws.receive_text()
    assert refused.value.code == 1008


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8765", "http://localhost:5173",
                                    "https://[::1]:8765"])
def test_a_loopback_host_and_origin_connect(tmp_path, monkeypatch, origin):
    _, client = _client(tmp_path, monkeypatch)
    _accepted(client, headers={"origin": origin})


def test_a_local_client_that_sends_no_origin_connects(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    _accepted(client)


def test_a_rebinding_host_is_refused(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch, base_url="http://evil.example:8765")
    _refused(client, headers={"origin": "http://evil.example:8765"})


def test_a_rebinding_host_is_refused_even_without_an_origin(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch, base_url="http://evil.example:8765")
    _refused(client)


@pytest.mark.parametrize("origin", ["http://evil.example", "http://localhost.evil.example",
                                    "null", "", "file://"])
def test_a_loopback_host_with_a_foreign_origin_is_refused(tmp_path, monkeypatch, origin):
    _, client = _client(tmp_path, monkeypatch)
    _refused(client, headers={"origin": origin})


def test_a_valid_token_connects_whatever_the_origin(tmp_path, monkeypatch):
    # The token path is untouched: a page on another site cannot obtain a
    # bootstrap token in the first place (it cannot read the response).
    channel, client = _client(tmp_path, monkeypatch)
    token = client.get("/webui/bootstrap").json()["token"]
    _accepted(client, f"/?token={token}", headers={"origin": "http://evil.example"})


def test_a_token_required_config_is_unchanged(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch, websocketRequiresToken=True)
    _refused(client)
    token = client.get("/webui/bootstrap").json()["token"]
    _accepted(client, f"/?token={token}", headers={"origin": "http://evil.example"})


def test_a_config_with_a_setup_secret_still_refuses_a_foreign_origin(tmp_path, monkeypatch):
    # A setup secret changes how a *token* is obtained (the bootstrap route
    # gates on it); it says nothing about an anonymous handshake, which
    # carries no token at all. Without this, a hand-written config pairing a
    # setup secret with websocketRequiresToken=false would accept an
    # anonymous socket from anywhere.
    _, client = _client(tmp_path, monkeypatch, base_url="https://durin.example.org",
                        tokenIssueSecret="s3cret")
    _refused(client, headers={"origin": "https://evil.example"})


def test_a_config_with_a_setup_secret_and_a_valid_token_is_unchanged(tmp_path, monkeypatch):
    # A deployment behind a setup secret still reaches the socket with a
    # bootstrap token, which a page on another site cannot obtain, so the
    # token path is unaffected by the loopback rule above.
    channel, client = _client(tmp_path, monkeypatch, base_url="https://durin.example.org",
                              tokenIssueSecret="s3cret")
    token = client.get("/webui/bootstrap", headers={"X-Durin-Auth": "s3cret"}).json()["token"]
    _accepted(client, f"/?token={token}", headers={"origin": "https://evil.example"})
