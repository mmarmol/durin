"""GET /api/v1/tools — the mode-editor tool catalog, through the real HTTP stack.

Exercises the Starlette front door end-to-end (routing + bearer auth + the
ModesService projection wired to a live-style tool registry resolver), which the
ModesService unit tests do not cover.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_api_app
from durin.security.api_tokens import ApiTokenStore
from durin.service.auth import AuthService
from durin.service.modes import ModesService
from durin.service.registry import ServiceRegistry

STATIC_TOKEN = "test-static-token"


class _FakeTool:
    def __init__(self, name: str, description: str, read_only: bool) -> None:
        self.name = name
        self.description = description
        self.read_only = read_only


class _FakeRegistry:
    """Stands in for the live loop's ToolRegistry (name lookup only)."""

    def __init__(self, *tools: _FakeTool) -> None:
        self._t = {t.name: t for t in tools}

    @property
    def tool_names(self) -> list[str]:
        return list(self._t)

    def get(self, name: str):
        return self._t.get(name)


@pytest.fixture()
def auth(tmp_path):
    return AuthService(store=ApiTokenStore(path=tmp_path / "tokens.json"))


@pytest.fixture()
def client(auth):
    registry = ServiceRegistry()
    live = _FakeRegistry(
        _FakeTool("read_file", "Read a file.", True),
        _FakeTool("edit_file", "Edit a file.", False),
        _FakeTool("mcp_srv_do", "An MCP tool.", True),
    )
    registry.register("modes", ModesService(tool_registry_resolver=lambda: live))
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token=STATIC_TOKEN)
    return TestClient(app, raise_server_exceptions=False)


def test_tools_catalog_returns_projected_live_registry(client):
    r = client.get("/api/v1/tools", headers={"Authorization": f"Bearer {STATIC_TOKEN}"})
    assert r.status_code == 200
    by_name = {t["name"]: t for t in r.json()["tools"]}
    assert by_name["read_file"] == {
        "name": "read_file",
        "description": "Read a file.",
        "read_only": True,
        "source": "builtin",
    }
    assert by_name["mcp_srv_do"]["source"] == "mcp"
    # Built-ins sort ahead of MCP tools.
    names = [t["name"] for t in r.json()["tools"]]
    assert names.index("edit_file") < names.index("mcp_srv_do")


def test_tools_catalog_requires_auth(client):
    assert client.get("/api/v1/tools").status_code == 401
