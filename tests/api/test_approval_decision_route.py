"""POST /api/v1/approvals/{id}/decision over HTTP, through the gateway app.

The dashboard's bootstrap token decides; a token issued for the API and the
static token are refused with 403 even with every scope, since a program's
input carries no authority to approve. A refused decision is a 409
problem+json whose ``details`` carry the outcome; an acted-on one is a 200.
"""

from __future__ import annotations

import importlib

import pytest
from starlette.testclient import TestClient

from durin.agent import approval_executors as ex
from durin.agent import approval_store as st
from durin.api.asgi import build_gateway_http_app
from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel
from durin.service.principal import Scope
from durin.session.manager import SessionManager

STATIC = "s3cr3t-static"


async def _run(ws, payload, deps):
    return {"ran": True}


@pytest.fixture(autouse=True)
def _fake_skill_edit(monkeypatch):
    for module in ex._KIND_MODULES:
        importlib.import_module(module)
    monkeypatch.setitem(ex._REGISTRY, "skill_edit", (lambda ws, payload: "h1", _run))


@pytest.fixture()
def gateway(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"], "host": "127.0.0.1", "port": 8765,
         "path": "/", "websocketRequiresToken": False, "token": STATIC},
        MessageBus(),
        session_manager=SessionManager(workspace),
    )
    registry = channel._services
    app = build_gateway_http_app(channel, registry, auth=registry.get("auth"),
                                 static_token=STATIC)
    return workspace, registry.get("auth"), TestClient(app)


def _record(ws, kind="skill_edit"):
    return st.create(ws, kind=kind, summary="edit skill 'a'", detail={}, payload={"name": "a"},
                     change_hash="h1", session_key="cron:nightly", context="autonomous")


def _dashboard_token(client) -> str:
    return client.get("/webui/bootstrap", headers={"X-Durin-Auth": STATIC}).json()["token"]


def _decide(client, token, approval_id, decision="approve"):
    return client.post(f"/api/v1/approvals/{approval_id}/decision",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"decision": decision})


def test_the_dashboard_session_decides(gateway):
    ws, _, client = gateway
    rec = _record(ws)

    resp = _decide(client, _dashboard_token(client), rec["id"])

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "applied", "message": "Done: edit skill 'a'.",
                           "approval_id": rec["id"]}
    assert st.get(ws, rec["id"])["decided_by"] == {"kind": "user", "channel": "webui"}


def test_an_api_token_is_refused_even_with_admin(gateway):
    ws, auth, client = gateway
    rec = _record(ws)
    _, api_token = auth._store.issue([Scope.ADMIN.value], label="automation-script")

    resp = _decide(client, api_token, rec["id"])

    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert st.get(ws, rec["id"])["status"] == "pending"


def test_the_static_token_is_refused(gateway):
    ws, _, client = gateway
    rec = _record(ws)

    resp = _decide(client, STATIC, rec["id"])

    assert resp.status_code == 403
    assert st.get(ws, rec["id"])["status"] == "pending"


def test_a_refused_decision_is_a_409_carrying_the_outcome(gateway):
    ws, _, client = gateway
    rec = _record(ws, kind="exec_command")

    resp = _decide(client, _dashboard_token(client), rec["id"])

    assert resp.status_code == 409
    details = resp.json()["details"]
    assert details["status"] == "refused" and details["approval_id"] == rec["id"]
    assert st.get(ws, rec["id"])["status"] == "pending"


def test_a_malformed_id_or_decision_is_a_422(gateway):
    _, _, client = gateway
    token = _dashboard_token(client)

    assert _decide(client, token, "not-an-id").status_code == 422
    assert _decide(client, token, "0123456789ab", decision="maybe").status_code == 422


def test_an_unknown_request_is_a_404(gateway):
    _, _, client = gateway

    assert _decide(client, _dashboard_token(client), "0123456789ab").status_code == 404
