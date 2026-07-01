"""Tests: webui-thread payload includes the active persona.

The endpoint GET /api/v1/sessions/{key}/webui-thread must include a `persona`
field in the returned payload:
  - set to session.metadata["persona"] when present
  - null when neither session metadata nor config default specifies one
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
from durin.utils.webui_transcript import append_transcript_object


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


def _seed_websocket_session(
    workspace: Path,
    key: str,
    persona: str | None = None,
) -> SessionManager:
    sm = SessionManager(workspace)
    s = Session(key=key)
    s.add_message("user", "hello")
    if persona is not None:
        s.metadata["persona"] = persona
    sm.save(s)
    # Write a JSONL transcript so build_webui_thread_response returns data (non-None path)
    append_transcript_object(key, {"event": "user", "chat_id": key.split(":")[-1], "text": "hello"})
    return sm


def test_webui_thread_includes_persona_from_session_metadata(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Payload `persona` equals the value stored in session metadata."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_websocket_session(tmp_path, key="websocket:p1", persona="socratic")
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    resp = client.get("/api/v1/sessions/websocket:p1/webui-thread", headers=auth)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["persona"] == "socratic"


def test_webui_thread_persona_is_null_when_no_persona_set(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Payload `persona` is null when neither session metadata nor config default set."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    # Stub load_config to return a config with no default persona
    mock_config = MagicMock()
    mock_config.agents.defaults.persona = None
    monkeypatch.setattr("durin.personas.resolve.load_config", lambda: mock_config, raising=False)
    monkeypatch.setattr("durin.api.asgi.load_config", lambda: mock_config, raising=False)

    sm = _seed_websocket_session(tmp_path, key="websocket:p2", persona=None)
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    resp = client.get("/api/v1/sessions/websocket:p2/webui-thread", headers=auth)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["persona"] is None
