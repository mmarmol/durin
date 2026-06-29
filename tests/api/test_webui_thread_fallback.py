"""Tests for the webui-thread endpoint's fallback to session history.

When a session has no webui JSONL transcript (non-websocket channels:
CLI, Telegram, subagent) the endpoint falls back to the universal
session history and returns 200 with UIMessage-shaped data instead of 404.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_gateway_http_app
from durin.channels.websocket import WebSocketChannel
from durin.session.manager import Session, SessionManager


def _ch(bus: Any, *, session_manager: SessionManager | None = None) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    return WebSocketChannel(cfg, bus, session_manager=session_manager)


def _make_client(bus: Any, *, session_manager: SessionManager | None = None) -> TestClient:
    channel = _ch(bus, session_manager=session_manager)
    registry = channel._services
    app = build_gateway_http_app(channel, registry, auth=registry.get("auth"))
    return TestClient(app)


def _token(client: TestClient) -> str:
    r = client.get("/webui/bootstrap")
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


def _seed_cli_session(workspace: Path, key: str = "cli:foo") -> SessionManager:
    sm = SessionManager(workspace)
    s = Session(key=key)
    s.add_message("user", "hello from cli")
    s.add_message("assistant", "I am here")
    sm.save(s)
    return sm


def test_nonwebsocket_session_without_transcript_returns_200(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI session with history but no webui JSONL returns 200, not 404."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_cli_session(tmp_path, key="cli:foo")
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    resp = client.get("/api/v1/sessions/cli:foo/webui-thread", headers=auth)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    # Must have the expected payload shape
    assert "messages" in data
    assert "schemaVersion" in data
    messages = data["messages"]
    assert len(messages) >= 2
    roles = [m["role"] for m in messages]
    assert "user" in roles
    assert "assistant" in roles
    # Must be marked read-only
    assert data.get("readOnly") is True


def test_nonwebsocket_session_messages_content(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The returned messages contain the actual conversation content."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_cli_session(tmp_path, key="cli:bar")
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    resp = client.get("/api/v1/sessions/cli:bar/webui-thread", headers=auth)
    assert resp.status_code == 200
    messages = resp.json()["data"]["messages"]
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert any("hello from cli" in m["content"] for m in user_msgs)


def test_nonexistent_key_still_returns_404(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key with neither a JSONL transcript nor a session file still returns 404."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    # Empty session manager — no sessions on disk
    sm = SessionManager(tmp_path)
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    resp = client.get("/api/v1/sessions/cli:ghost/webui-thread", headers=auth)
    assert resp.status_code == 404


def test_websocket_session_with_transcript_uses_jsonl(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A websocket session with a JSONL transcript is NOT marked readOnly."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    from durin.utils.webui_transcript import append_transcript_object

    sm = SessionManager(tmp_path)
    s = Session(key="websocket:live")
    s.add_message("user", "hi")
    sm.save(s)
    append_transcript_object(
        "websocket:live", {"event": "user", "chat_id": "live", "text": "hi"}
    )

    client = _make_client(bus, session_manager=sm)
    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    resp = client.get("/api/v1/sessions/websocket:live/webui-thread", headers=auth)
    assert resp.status_code == 200
    data = resp.json()["data"]
    # Normal websocket sessions are NOT read-only
    assert not data.get("readOnly")
