"""GET /api/v1/pending over HTTP, through the gateway app."""

from __future__ import annotations

from starlette.testclient import TestClient

from durin.agent import approval_store as st
from durin.api.asgi import build_gateway_http_app
from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel
from durin.service.principal import Scope
from durin.session.manager import SessionManager


def _gateway(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"], "host": "127.0.0.1", "port": 8765,
         "path": "/", "websocketRequiresToken": False},
        MessageBus(),
        session_manager=SessionManager(workspace),
    )
    registry = channel._services
    app = build_gateway_http_app(channel, registry, auth=registry.get("auth"))
    return workspace, registry.get("auth"), TestClient(app)


def test_the_dashboard_lists_what_waits_on_a_person(tmp_path, monkeypatch):
    ws, _, client = _gateway(tmp_path, monkeypatch)
    rec = st.create(ws, kind="skill_install", summary="install skill 'demo'", detail={},
                    payload={}, change_hash="h", session_key="cron:x", context="autonomous")
    token = client.get("/webui/bootstrap").json()["token"]

    resp = client.get("/api/v1/pending", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1 and body["errors"] == []
    item = body["items"][0]
    assert (item["source"], item["id"], item["kind"]) == ("approval", rec["id"], "skill_install")
    assert item["resolve"]["actions"][0]["path"] == f"/api/v1/approvals/{rec['id']}/decision"


def test_a_token_that_can_read_no_source_is_refused(tmp_path, monkeypatch):
    _, auth, client = _gateway(tmp_path, monkeypatch)
    _, token = auth._store.issue([Scope.CHAT_WRITE.value], label="chatbot")

    resp = client.get("/api/v1/pending", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 403
