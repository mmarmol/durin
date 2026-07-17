"""Tests for the webui-thread endpoint's ``before`` cursor param and the
agent-session fallback conversion cache.

- ``before`` propagates from the query string to ``build_webui_thread_response``
  and the response's ``data.prevCursor`` chains back to ``null``.
- An invalid ``before`` (non-integer or negative) is rejected before any
  service/service call, via the repo's ``ValidationFailedError`` problem
  response.
- The agent-session fallback conversion (used when no webui JSONL transcript
  exists) is cached by session-file identity so unchanged repeat reads don't
  re-run the conversion; a session-file mtime bump invalidates the cache.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_gateway_http_app
from durin.channels.websocket import WebSocketChannel
from durin.session.manager import Session, SessionManager
from durin.utils import webui_transcript as wt
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


def _seed_cli_session(workspace: Path, key: str = "cli:foo") -> SessionManager:
    sm = SessionManager(workspace)
    s = Session(key=key)
    s.add_message("user", "hello from cli")
    s.add_message("assistant", "I am here")
    sm.save(s)
    return sm


def _seed_paged_transcript(key: str, turns: int) -> None:
    """Write ``turns`` user/message/turn_end records in writer-faithful shape.

    The boundary scan (``_is_user_line``) matches on top-level ``"event":"user"``;
    padding the assistant row keeps each turn a few hundred bytes so a handful of
    turns exceed a shrunk page size.
    """
    for i in range(turns):
        append_transcript_object(
            key, {"event": "user", "chat_id": "x", "text": f"question {i}"}
        )
        append_transcript_object(key, {"event": "message", "chat_id": "x", "kind": "tool_hint", "text": f"tool run {i}"})
        append_transcript_object(
            key, {"event": "message", "chat_id": "x", "text": f"answer {i} " + "x" * 200}
        )
        append_transcript_object(key, {"event": "turn_end", "chat_id": "x"})


def test_thread_endpoint_accepts_before_and_returns_prev_cursor(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    # Shrink the page-size default so a modest transcript spans several pages
    # instead of requiring megabytes of test data.
    monkeypatch.setattr(
        wt, "read_transcript_page", functools.partial(wt.read_transcript_page, target_bytes=1500)
    )

    key = "websocket:paged"
    _seed_paged_transcript(key, turns=20)

    client = _make_client(bus)
    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    resp = client.get(f"/api/v1/sessions/{key}/webui-thread", headers=auth)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert isinstance(data["prevCursor"], int)

    # Walk the chain back to the start; it must terminate at null.
    cursor = data["prevCursor"]
    seen_null = False
    for _ in range(50):
        resp = client.get(
            f"/api/v1/sessions/{key}/webui-thread", params={"before": cursor}, headers=auth
        )
        assert resp.status_code == 200, resp.text
        page = resp.json()["data"]
        assert isinstance(page["messages"], list)
        if page["prevCursor"] is None:
            seen_null = True
            break
        cursor = page["prevCursor"]
    assert seen_null, "pagination chain never reached null prevCursor"


@pytest.mark.parametrize("before_raw", ["not-an-int", "-1"])
def test_invalid_before_is_rejected(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before_raw: str
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:badbefore"
    append_transcript_object(key, {"event": "user", "chat_id": "x", "text": "hi"})

    client = _make_client(bus)
    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    resp = client.get(
        f"/api/v1/sessions/{key}/webui-thread", params={"before": before_raw}, headers=auth
    )
    assert resp.status_code == 422, resp.text
    problem = resp.json()
    assert problem["type"] == "urn:durin:error:validation_failed"


def test_fallback_conversion_is_cached(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI session without a webui transcript is converted once and cached."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_cli_session(tmp_path, key="cli:cache")
    client = _make_client(bus, session_manager=sm)
    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    original = wt.session_messages_to_ui_messages
    calls: list[int] = []

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(wt, "session_messages_to_ui_messages", counting)

    resp1 = client.get("/api/v1/sessions/cli:cache/webui-thread", headers=auth)
    assert resp1.status_code == 200, resp1.text
    resp2 = client.get("/api/v1/sessions/cli:cache/webui-thread", headers=auth)
    assert resp2.status_code == 200, resp2.text
    assert len(calls) == 1, "second read should be served from the fallback cache"
    assert resp2.json()["data"]["prevCursor"] is None

    # Bump the session file's mtime (simulating a new turn) — must invalidate.
    path = sm._get_session_path("cli:cache")
    stat = path.stat()
    os.utime(path, (stat.st_mtime + 5, stat.st_mtime + 5))

    resp3 = client.get("/api/v1/sessions/cli:cache/webui-thread", headers=auth)
    assert resp3.status_code == 200, resp3.text
    assert len(calls) == 2, "an mtime bump must invalidate the cached conversion"
