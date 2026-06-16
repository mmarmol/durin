"""MCP routes mount on the Starlette front door and map domain errors to
problem+json."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_api_app
from durin.security.api_tokens import ApiTokenStore
from durin.service.auth import AuthService
from durin.service.mcp import McpService
from durin.service.registry import ServiceRegistry

STATIC_TOKEN = "test-static-token"
AUTH_HEADER = {"Authorization": f"Bearer {STATIC_TOKEN}"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate the config the MCP service reads/writes.
    from durin.config.loader import save_config
    from durin.config.schema import Config

    cfg_path = tmp_path / "config.json"
    save_config(Config(), cfg_path)
    monkeypatch.setattr("durin.config.loader._current_config_path", cfg_path)

    auth = AuthService(store=ApiTokenStore(path=tmp_path / "tokens.json"))
    registry = ServiceRegistry()
    registry.register("mcp", McpService())
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token=STATIC_TOKEN)
    return TestClient(app, raise_server_exceptions=False)


def test_list_route_mounted(client):
    r = client.get("/api/v1/mcp/servers", headers=AUTH_HEADER)
    assert r.status_code == 200
    assert r.json() == {"servers": []}


def test_unauthenticated_is_401(client):
    r = client.get("/api/v1/mcp/servers")
    assert r.status_code == 401


def test_get_unknown_server_is_problem_json_404(client):
    r = client.get("/api/v1/mcp/servers/ghost", headers=AUTH_HEADER)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["details"] == {"name": "ghost"}


def test_delete_unknown_server_is_404(client):
    r = client.request(
        "DELETE", "/api/v1/mcp/servers/ghost", headers=AUTH_HEADER, json={}
    )
    assert r.status_code == 404
