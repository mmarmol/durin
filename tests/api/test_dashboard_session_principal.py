"""A dashboard session is told apart from an API token.

The token ``/webui/bootstrap`` mints for the dashboard resolves to a
``webui`` principal. Every token issued for the API (``durin auth token
issue``, ``POST /api/v1/auth/tokens``) and the configured static token resolve
to a ``remote`` one, whatever their scopes or label, so a route that needs a
person (deciding an approval) can refuse a program.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from durin.api.asgi import build_gateway_http_app, resolve_principal_from_headers
from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel
from durin.service.auth import IssueTokenCommand
from durin.service.principal import Principal, Scope


def _gateway(tmp_path, monkeypatch, **cfg_extra: Any):
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
        **cfg_extra,
    }
    channel = WebSocketChannel(cfg, MessageBus())
    registry = channel._services
    auth = registry.get("auth")
    app = build_gateway_http_app(
        channel, registry, auth=auth, static_token=cfg_extra.get("token", ""))
    return auth, TestClient(app)


def _resolve(auth, token: str, static_token: str = "") -> Principal | None:
    return resolve_principal_from_headers(
        {"authorization": f"Bearer {token}"}, auth=auth, static_token=static_token)


def test_the_bootstrap_token_is_a_dashboard_session(tmp_path, monkeypatch) -> None:
    auth, client = _gateway(tmp_path, monkeypatch)
    token = client.get("/webui/bootstrap").json()["token"]

    principal = _resolve(auth, token)

    assert principal is not None
    assert principal.kind == "webui"
    assert principal.has_scope(Scope.ADMIN)


def test_an_issued_token_is_not_a_dashboard_session_even_labelled_bootstrap(
    tmp_path, monkeypatch,
) -> None:
    auth, _ = _gateway(tmp_path, monkeypatch)
    _, token = auth._store.issue([Scope.ADMIN.value], label="bootstrap")

    principal = _resolve(auth, token)

    assert principal is not None
    assert principal.kind == "remote"


def test_the_static_token_is_not_a_dashboard_session(tmp_path, monkeypatch) -> None:
    auth, _ = _gateway(tmp_path, monkeypatch, token="s3cr3t-static")

    principal = _resolve(auth, "s3cr3t-static", static_token="s3cr3t-static")

    assert principal is not None
    assert principal.kind == "remote"


def test_the_token_api_cannot_mint_a_dashboard_session() -> None:
    # The kind is set by the server path that mints the token, never by the
    # caller: the issue command rejects the field outright.
    with pytest.raises(ValidationError):
        IssueTokenCommand(scopes=[Scope.ADMIN.value], kind="webui")


def test_the_tokens_listing_shows_which_tokens_are_dashboard_sessions(
    tmp_path, monkeypatch,
) -> None:
    auth, client = _gateway(tmp_path, monkeypatch)
    client.get("/webui/bootstrap")
    auth._store.issue([Scope.SKILLS_READ.value], label="api-client")

    kinds = sorted((t["label"], t["kind"]) for t in auth._store.list_tokens())

    assert kinds == [("api-client", "remote"), ("bootstrap", "webui")]
