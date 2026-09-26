"""Only a dashboard session decides an approval over the webui socket.

The dashboard opens its socket with the single-use token ``/webui/bootstrap``
just minted, and that connection's Approve / Reject clicks decide. A socket
opened with the configured static token (a script holding the operator's
secret) or with no token at all (``websocket_requires_token`` off) can still
chat, but its ``approval_decision`` frames are refused and the request is left
as it was.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from durin.agent import approval_store
from durin.api.asgi import build_gateway_http_app
from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel
from durin.session.manager import SessionManager


def _gateway(tmp_path, monkeypatch, **cfg_extra):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = {"enabled": True, "allowFrom": ["*"], "host": "127.0.0.1", "port": 8765,
           "path": "/", "websocketRequiresToken": True, **cfg_extra}
    channel = WebSocketChannel(cfg, MessageBus(), session_manager=SessionManager(workspace))
    registry = channel._services
    app = build_gateway_http_app(channel, registry, auth=registry.get("auth"),
                                 static_token=cfg_extra.get("token", ""))
    return workspace, TestClient(app)


def _reject_over_socket(client: TestClient, path: str, workspace) -> tuple[dict, dict]:
    """Open the socket at *path*, file a request for its chat, send a Reject
    click for it, and return the ``approval_decided`` reply and the record."""
    with client.websocket_connect(path) as ws:
        ready = ws.receive_json()
        assert ready["event"] == "ready"
        rec = approval_store.create(
            workspace, kind="skill_edit", summary="edit skill 'a'", detail={},
            payload={"name": "a"}, change_hash="h",
            session_key=f"websocket:{ready['chat_id']}", context="interactive")
        ws.send_json({"type": "approval_decision", "request_id": "r1",
                      "approval_id": rec["id"], "decision": "reject"})
        while True:
            frame = ws.receive_json()
            if frame.get("event") == "approval_decided":
                return frame, rec


def test_a_dashboard_session_decides(tmp_path, monkeypatch):
    workspace, client = _gateway(tmp_path, monkeypatch)
    token = client.get("/webui/bootstrap").json()["token"]

    reply, rec = _reject_over_socket(client, f"/?token={token}", workspace)

    assert reply["ok"] is True and reply["status"] == "rejected"
    assert approval_store.get(workspace, rec["id"])["status"] == "rejected"


def test_a_static_token_socket_cannot_decide(tmp_path, monkeypatch):
    workspace, client = _gateway(tmp_path, monkeypatch, token="s3cr3t-static")

    reply, rec = _reject_over_socket(client, "/?token=s3cr3t-static", workspace)

    assert reply["ok"] is False and reply["status"] == "refused"
    assert "dashboard session" in reply["message"]
    assert approval_store.get(workspace, rec["id"])["status"] == "pending"


def test_an_anonymous_socket_cannot_decide(tmp_path, monkeypatch):
    workspace, client = _gateway(tmp_path, monkeypatch, websocketRequiresToken=False)

    reply, rec = _reject_over_socket(client, "/", workspace)

    assert reply["ok"] is False and reply["status"] == "refused"
    assert approval_store.get(workspace, rec["id"])["status"] == "pending"


def test_a_spent_bootstrap_token_opens_an_anonymous_socket_that_cannot_decide(
    tmp_path, monkeypatch,
):
    # With no token required, a token already used for a handshake opens the
    # socket anonymously; only a fresh one opens a dashboard session.
    workspace, client = _gateway(tmp_path, monkeypatch, websocketRequiresToken=False)
    token = client.get("/webui/bootstrap").json()["token"]
    with client.websocket_connect(f"/?token={token}") as ws:
        ws.receive_json()

    reply, _ = _reject_over_socket(client, f"/?token={token}", workspace)

    assert reply["status"] == "refused"
