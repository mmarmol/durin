"""Gateway OpenAI-compatible /v1 surface: auth gating and models listing."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from durin.bus.queue import MessageBus


def _make_loop(text: str = "mock response") -> MagicMock:
    loop = MagicMock()
    loop.process_direct = AsyncMock(return_value=SimpleNamespace(content=text))
    return loop


def _build_app(tmp_path, monkeypatch, agent_loop=None, api_request_timeout: float = 5.0):
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    media_dir = tmp_path / "media"
    media_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "durin.api.openai_routes.get_media_dir", lambda _channel: media_dir
    )

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
    return build_gateway_http_app(
        channel,
        registry,
        auth=registry.get("auth"),
        agent_loop=agent_loop if agent_loop is not None else _make_loop(),
        model_name="test-model",
        api_request_timeout=api_request_timeout,
    )


def _mint(scopes: list[str]) -> str:
    from durin.security.api_tokens import ApiTokenStore

    _token_id, plaintext = ApiTokenStore().issue(scopes, label="test")
    return plaintext


@pytest.fixture()
def client(tmp_path, monkeypatch):
    return TestClient(_build_app(tmp_path, monkeypatch))


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_models_requires_token(client):
    r = client.get("/v1/models")
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"


def test_models_rejects_garbage_token(client):
    r = client.get("/v1/models", headers=_hdr("nbwt_not_a_real_token"))
    assert r.status_code == 401


def test_models_rejects_wrong_scope(client):
    tok = _mint(["sessions:read"])
    r = client.get("/v1/models", headers=_hdr(tok))
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "permission_error"


def test_models_ok_with_chat_write(client):
    tok = _mint(["chat:write"])
    r = client.get("/v1/models", headers=_hdr(tok))
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list"
    assert data["data"][0]["id"] == "test-model"
    assert data["data"][0]["owned_by"] == "durin"


def test_models_ok_with_admin(client):
    tok = _mint(["admin"])
    r = client.get("/v1/models", headers=_hdr(tok))
    assert r.status_code == 200


def test_chat_requires_token(client):
    r = client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 401


def test_v1_absent_when_no_agent_loop(tmp_path, monkeypatch):
    from durin.api.asgi import build_gateway_http_app
    from durin.channels.websocket import WebSocketChannel

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
    }
    channel = WebSocketChannel(cfg, MessageBus())
    app = build_gateway_http_app(
        channel, channel._services, auth=channel._services.get("auth")
    )
    client = TestClient(app)
    r = client.get("/v1/models")
    assert r.status_code == 404
