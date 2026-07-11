"""build_api_app — GET /api/v1/workflows/scripts must not be shadowed by (and
must not shadow) GET /api/v1/workflows/{name}.

A workflow can legitimately be named "scripts"; the static /workflows/scripts
route must still win over the {name} parameter route for that literal path,
the same way /workflows/runs already does (see durin/api/asgi.py's
_route_order sort in build_api_app)."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_api_app
from durin.security.api_tokens import ApiTokenStore
from durin.service.auth import AuthService
from durin.service.registry import ServiceRegistry
from durin.service.workflows import WorkflowsService

STATIC_TOKEN = "test-static-token"


@pytest.fixture()
def token_store(tmp_path):
    return ApiTokenStore(path=tmp_path / "tokens.json")


@pytest.fixture()
def auth(token_store):
    return AuthService(store=token_store)


@pytest.fixture()
def workspace(tmp_path):
    return tmp_path / "ws"


@pytest.fixture()
def registry(auth, workspace):
    reg = ServiceRegistry()
    reg.register("auth", auth)
    reg.register("workflows", WorkflowsService(workspace=workspace))
    return reg


@pytest.fixture()
def client(registry, auth):
    app = build_api_app(registry, auth=auth, static_token=STATIC_TOKEN)
    return TestClient(app, raise_server_exceptions=False)


def _auth_headers():
    return {"Authorization": f"Bearer {STATIC_TOKEN}"}


def test_scripts_route_wins_over_a_workflow_literally_named_scripts(client, workspace):
    # A workflow file named "scripts.json" — if {name} shadowed the static route,
    # GET /api/v1/workflows/scripts would return this workflow's definition instead.
    workflows_dir = workspace / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "scripts.json").write_text(
        json.dumps({"name": "scripts", "start": "a", "nodes": [{"id": "a", "kind": "work"}]})
    )
    scripts_dir = workflows_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "check.py").write_text("print('hi')\n")

    resp = client.get("/api/v1/workflows/scripts", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    # The scripts-listing shape, not the workflow-get shape (which would carry
    # "definition" and "name" for the workflow literally named "scripts").
    assert body == {"scripts": ["check.py"]}
    assert "definition" not in body


def test_workflow_named_scripts_is_still_reachable_by_its_own_route(client, workspace):
    # The {name} route is unaffected — a workflow named "scripts" is still
    # readable, just not via GET /api/v1/workflows/scripts.
    workflows_dir = workspace / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "scripts.json").write_text(
        json.dumps({"name": "scripts", "start": "a", "nodes": [{"id": "a", "kind": "work"}]})
    )

    resp = client.get("/api/v1/workflows/scripts", headers=_auth_headers())
    assert resp.json() == {"scripts": []}   # no workflows/scripts/ dir in this test

    # list() still surfaces it as a workflow name (proves the file itself is intact)
    listing = client.get("/api/v1/workflows", headers=_auth_headers())
    assert "scripts" in listing.json()["workflows"]
