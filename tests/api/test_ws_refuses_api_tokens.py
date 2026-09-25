"""An API token never opens the webui socket.

The webui's Approve / Reject click travels on the socket as an
``approval_decision`` frame, and a click is the person's decision. A token
issued for the API (``chat:write`` lets a program hold a conversation over
HTTP) must not carry that authority, so it must not be able to open the socket
at all. The handshake takes only the configured static token or a single-use
token minted by ``/webui/bootstrap``; it never consults the API token store.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from durin.bus.queue import MessageBus
from durin.service.principal import Scope


def _app(tmp_path, monkeypatch, **cfg_extra):
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
        **cfg_extra,
    }
    channel = WebSocketChannel(cfg, MessageBus())
    registry = channel._services
    auth = registry.get("auth")
    app = build_gateway_http_app(
        channel, registry, auth=auth, static_token=cfg_extra.get("token", ""))
    return auth, TestClient(app, raise_server_exceptions=False)


def _chat_write_token(auth) -> str:
    from durin.api.asgi import resolve_principal_from_headers

    _, token = auth._store.issue([Scope.CHAT_WRITE.value], label="api-client")
    # A live token: the HTTP front door accepts it for the chat routes.
    principal = resolve_principal_from_headers({"authorization": f"Bearer {token}"}, auth=auth)
    assert principal is not None and principal.has_scope(Scope.CHAT_WRITE)
    return token


def _refused(client, path: str, headers: dict | None = None) -> None:
    # The server closes with 1008 before accept; the test client raises.
    with pytest.raises(Exception):
        with client.websocket_connect(path, headers=headers or {}) as ws:
            ws.receive_text()


@pytest.mark.parametrize("cfg", [{}, {"token": "s3cr3t-static"}])
def test_a_chat_write_token_cannot_open_the_socket(tmp_path, monkeypatch, cfg) -> None:
    auth, client = _app(tmp_path, monkeypatch, **cfg)
    token = _chat_write_token(auth)
    _refused(client, f"/?token={token}")
    _refused(client, "/", headers={"Authorization": f"Bearer {token}"})


def test_the_dashboard_s_bootstrap_token_still_opens_it(tmp_path, monkeypatch) -> None:
    # The refusal above is specific to API tokens, not a closed socket.
    _, client = _app(tmp_path, monkeypatch)
    token = client.get("/webui/bootstrap").json()["token"]
    with client.websocket_connect(f"/?token={token}") as ws:
        assert ws.receive_json()["event"] == "ready"
