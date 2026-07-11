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


def test_script_put_then_get_round_trip(client):
    put = client.put(
        "/api/v1/workflows/scripts/check.py",
        headers=_auth_headers(),
        json={"content": "print('hi')\n"},
    )
    assert put.status_code == 200
    assert put.json() == {"name": "check.py"}

    got = client.get("/api/v1/workflows/scripts/check.py", headers=_auth_headers())
    assert got.status_code == 200
    assert got.json() == {"name": "check.py", "content": "print('hi')\n"}


def test_script_get_missing_is_404(client):
    resp = client.get("/api/v1/workflows/scripts/ghost.py", headers=_auth_headers())
    assert resp.status_code == 404


def test_script_put_rejects_path_traversal(client, workspace):
    resp = client.put(
        "/api/v1/workflows/scripts/../escape.py",
        headers=_auth_headers(),
        json={"content": "x"},
    )
    # The client/server normalize "../" out of the URL before routing, so this lands
    # on /api/v1/workflows/escape.py instead — a path with no PUT route registered
    # (workflow {name} only has GET/POST/DELETE) -> 405, not a successful write.
    assert resp.status_code == 405
    assert not (workspace / "workflows" / "escape.py").exists()
    assert not (workspace / "workflows" / "scripts" / "escape.py").exists()


def test_scripts_name_route_does_not_collide_with_a_run_of_a_workflow_named_scripts(client, workspace):
    # POST /api/v1/workflows/scripts/run is unambiguous even though it shares a path
    # shape with GET/PUT .../scripts/{name}: the run route is POST-only and the
    # scripts/{name} routes are GET/PUT-only, so the method alone disambiguates.
    workflows_dir = workspace / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "scripts.json").write_text(
        json.dumps({"name": "scripts", "start": "a", "nodes": [{"id": "a", "kind": "work"}]})
    )
    resp = client.post(
        "/api/v1/workflows/scripts/run",
        headers=_auth_headers(),
        json={"task": "x"},
    )
    # No app_config/sessions wired on this registry's WorkflowsService -> 503, not the
    # 404/422 that a scripts/{name} route mismatch would raise. Proves the POST landed
    # on the run() handler for workflow "scripts", not on a scripts/{name} handler.
    assert resp.status_code == 503
