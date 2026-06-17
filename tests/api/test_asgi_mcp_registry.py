"""E2E: MCP registry discovery routes over the real Starlette app (TestClient).

Exercises the exact HTTP stack the webUI calls — routing, Bearer auth, query/command
parsing, the McpService, and the install→add→list flow — with a deterministic fake
adapter (no network). The live-registry path is covered by the `durin mcp search` CLI
E2E.
"""
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


class _FakeReg:
    name = "official"

    async def search(self, query, *, limit):
        from durin.agent.mcp_registry import _hit_from_server

        return [
            _hit_from_server(
                {"name": "io.x/jira", "description": "Jira", "remotes": [{}]},
                registry="official",
            )
        ]

    async def describe(self, ref):
        from durin.agent.mcp_registry import parse_server_json

        return parse_server_json({
            "name": ref, "version": "2.0.0",
            "remotes": [{"type": "streamable-http", "url": "https://m/jira"}],
        })


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from durin.config.loader import save_config
    from durin.config.schema import Config

    cfg_path = tmp_path / "config.json"
    save_config(Config(), cfg_path)
    monkeypatch.setattr("durin.config.loader._current_config_path", cfg_path)
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeReg()]
    )

    auth = AuthService(store=ApiTokenStore(path=tmp_path / "tokens.json"))
    registry = ServiceRegistry()
    registry.register("mcp", McpService())
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token=STATIC_TOKEN)
    return TestClient(app, raise_server_exceptions=False)


def test_registry_search_over_http(client):
    r = client.get("/api/v1/mcp/registry/search?q=jira&limit=5", headers=AUTH_HEADER)
    assert r.status_code == 200
    assert r.json()["hits"][0]["ref"] == "io.x/jira"


def test_registry_search_requires_auth(client):
    assert client.get("/api/v1/mcp/registry/search?q=jira").status_code == 401


def test_registry_describe_over_http(client):
    r = client.get("/api/v1/mcp/registry/describe?ref=io.x/jira", headers=AUTH_HEADER)
    assert r.status_code == 200
    assert r.json()["version"] == "2.0.0"


def test_registry_install_over_http_then_listed(client):
    r = client.post(
        "/api/v1/mcp/registry/install",
        headers=AUTH_HEADER,
        json={"ref": "io.x/jira", "prefer": "remote"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "jira"
    assert r.json()["config"]["url"] == "https://m/jira"

    listed = client.get("/api/v1/mcp/servers", headers=AUTH_HEADER)
    names = [s["name"] for s in listed.json()["servers"]]
    assert "jira" in names
