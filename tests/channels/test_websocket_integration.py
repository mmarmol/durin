"""Integration tests for the WebSocket channel using the unified ASGI app in-process.

Drives the unified Starlette app (build_gateway_http_app) via TestClient so no
real socket is opened.  The chat-protocol assertions (ready, delta, stream_end,
message, etc.) are unchanged; only the connection mechanism is different.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_gateway_http_app
from durin.bus.events import OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel


def _make_channel(tmp_path: Any, monkeypatch: Any, **kw: Any) -> WebSocketChannel:
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    cfg.update(kw)
    return WebSocketChannel(cfg, MessageBus())


def _make_client(ch: WebSocketChannel) -> TestClient:
    registry = ch._services
    auth = registry.get("auth")
    app = build_gateway_http_app(ch, registry, auth=auth)
    return TestClient(app, raise_server_exceptions=False)


def _build(ch: WebSocketChannel, **kw: Any) -> TestClient:
    """Build a TestClient; kw forwarded to TestClient constructor."""
    registry = ch._services
    auth = registry.get("auth")
    app = build_gateway_http_app(ch, registry, auth=auth)
    return TestClient(app, **kw)


def _ws_path(ch: WebSocketChannel) -> str:
    return ch._expected_path() or "/"


def _connect(client: TestClient, ch: WebSocketChannel, *, token: str = "", client_id: str = "") -> Any:
    """Return a websocket_connect context manager on the channel's WS path."""
    path = _ws_path(ch)
    params: list[str] = []
    if client_id:
        params.append(f"client_id={client_id}")
    if token:
        params.append(f"token={token}")
    if params:
        path = path + "?" + "&".join(params)
    return client.websocket_connect(path)


def _recv(ws: Any) -> dict[str, Any]:
    return json.loads(ws.receive_text())


def _bootstrap_token(client: TestClient) -> str:
    r = client.get("/webui/bootstrap")
    assert r.status_code == 200
    return r.json()["token"]


# -- Connection basics --------------------------------------------------------


def test_ready_event_fields(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    client = _build(ch)
    with _connect(client, ch, client_id="c1") as ws:
        r = _recv(ws)
        assert r["event"] == "ready"
        assert len(r["chat_id"]) == 36
        assert r["client_id"] == "c1"


def test_anonymous_client_gets_generated_id(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    client = _build(ch)
    with _connect(client, ch) as ws:
        r = _recv(ws)
        assert r["client_id"].startswith("anon-")


def test_each_connection_unique_chat_id(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    client = _build(ch)
    with _connect(client, ch, client_id="a") as ws1:
        r1 = _recv(ws1)
        with _connect(client, ch, client_id="b") as ws2:
            r2 = _recv(ws2)
            assert r1["chat_id"] != r2["chat_id"]


# -- Inbound messages (client -> server) --------------------------------------


def test_plain_text(tmp_path, monkeypatch):
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    ch = WebSocketChannel(cfg, bus)
    client = _build(ch)

    with _connect(client, ch, client_id="p") as ws:
        _recv(ws)  # ready
        ws.send_text("hello world")
        time.sleep(0.05)
        inbound = bus.publish_inbound.call_args[0][0]
        assert inbound.content == "hello world"
        assert inbound.sender_id == "p"


def test_json_content_field(tmp_path, monkeypatch):
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    ch = WebSocketChannel(cfg, bus)
    client = _build(ch)

    with _connect(client, ch, client_id="j") as ws:
        _recv(ws)  # ready
        ws.send_json({"content": "structured"})
        time.sleep(0.05)
        assert bus.publish_inbound.call_args[0][0].content == "structured"


def test_json_text_and_message_fields(tmp_path, monkeypatch):
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    ch = WebSocketChannel(cfg, bus)
    client = _build(ch)

    with _connect(client, ch, client_id="x") as ws:
        _recv(ws)  # ready
        ws.send_json({"text": "via text"})
        time.sleep(0.05)
        assert bus.publish_inbound.call_args[0][0].content == "via text"
        ws.send_json({"message": "via message"})
        time.sleep(0.05)
        assert bus.publish_inbound.call_args[0][0].content == "via message"


def test_empty_payload_ignored(tmp_path, monkeypatch):
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    ch = WebSocketChannel(cfg, bus)
    client = _build(ch)

    with _connect(client, ch, client_id="e") as ws:
        _recv(ws)  # ready
        ws.send_text("   ")
        ws.send_json({})
        time.sleep(0.05)
        bus.publish_inbound.assert_not_awaited()


def test_messages_preserve_order(tmp_path, monkeypatch):
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    ch = WebSocketChannel(cfg, bus)
    client = _build(ch)

    with _connect(client, ch, client_id="o") as ws:
        _recv(ws)  # ready
        for i in range(5):
            ws.send_text(f"msg-{i}")
        time.sleep(0.1)
        contents = [call[0][0].content for call in bus.publish_inbound.call_args_list]
        assert contents == [f"msg-{i}" for i in range(5)]


# -- Outbound messages (server -> client) -------------------------------------


def test_server_send_message(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    client = _build(ch)

    with _connect(client, ch, client_id="r") as ws:
        ready = _recv(ws)
        asyncio.run(ch.send(OutboundMessage(
            channel="websocket", chat_id=ready["chat_id"], content="reply",
        )))
        msg = _recv(ws)
        assert msg["event"] == "message"
        assert msg["text"] == "reply"


def test_server_send_tags_tool_hint_with_kind(tmp_path, monkeypatch):
    """_tool_hint metadata must surface as kind: "tool_hint" so WS
    clients render breadcrumbs separately from conversational replies."""
    ch = _make_channel(tmp_path, monkeypatch)
    client = _build(ch)

    with _connect(client, ch, client_id="h") as ws:
        ready = _recv(ws)
        chat_id = ready["chat_id"]

        # Plain reply: no "kind" field.
        asyncio.run(ch.send(OutboundMessage(
            channel="websocket", chat_id=chat_id, content="hi",
        )))
        plain = _recv(ws)
        assert plain.get("kind") is None

        # Tool-hint breadcrumb: kind == "tool_hint".
        asyncio.run(ch.send(OutboundMessage(
            channel="websocket", chat_id=chat_id,
            content='weather("get")',
            metadata={"_progress": True, "_tool_hint": True},
        )))
        hint = _recv(ws)
        assert hint.get("kind") == "tool_hint"
        assert hint["text"] == 'weather("get")'

        # Generic progress (non-tool-hint) gets the softer "progress" label.
        asyncio.run(ch.send(OutboundMessage(
            channel="websocket", chat_id=chat_id,
            content="thinking…",
            metadata={"_progress": True},
        )))
        prog = _recv(ws)
        assert prog.get("kind") == "progress"


def test_server_send_with_media_and_reply(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    client = _build(ch)

    with _connect(client, ch, client_id="m") as ws:
        ready = _recv(ws)
        asyncio.run(ch.send(OutboundMessage(
            channel="websocket", chat_id=ready["chat_id"], content="img",
            media=["/tmp/a.png"], reply_to="m1",
        )))
        msg = _recv(ws)
        assert msg["text"] == "img"
        assert msg["media"] == ["/tmp/a.png"]
        assert msg["reply_to"] == "m1"


# -- Streaming ----------------------------------------------------------------


def test_streaming_deltas_and_end(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch, streaming=True)
    client = _build(ch)

    with _connect(client, ch, client_id="s") as ws:
        cid = _recv(ws)["chat_id"]
        for part in ("Hello", " ", "world", "!"):
            asyncio.run(ch.send_delta(cid, part, {"_stream_delta": True, "_stream_id": "s1"}))
        asyncio.run(ch.send_delta(cid, "", {"_stream_end": True, "_stream_id": "s1"}))

        msgs = []
        while True:
            m = _recv(ws)
            msgs.append(m)
            if m["event"] == "stream_end":
                break

        deltas = [m for m in msgs if m["event"] == "delta"]
        assert "".join(d["text"] for d in deltas) == "Hello world!"
        ends = [m for m in msgs if m["event"] == "stream_end"]
        assert len(ends) == 1


def test_interleaved_streams(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch, streaming=True)
    client = _build(ch)

    with _connect(client, ch, client_id="i") as ws:
        cid = _recv(ws)["chat_id"]
        asyncio.run(ch.send_delta(cid, "A1", {"_stream_delta": True, "_stream_id": "sa"}))
        asyncio.run(ch.send_delta(cid, "B1", {"_stream_delta": True, "_stream_id": "sb"}))
        asyncio.run(ch.send_delta(cid, "A2", {"_stream_delta": True, "_stream_id": "sa"}))
        asyncio.run(ch.send_delta(cid, "", {"_stream_end": True, "_stream_id": "sa"}))
        asyncio.run(ch.send_delta(cid, "B2", {"_stream_delta": True, "_stream_id": "sb"}))
        asyncio.run(ch.send_delta(cid, "", {"_stream_end": True, "_stream_id": "sb"}))

        msgs = [_recv(ws) for _ in range(6)]
        sa = "".join(m["text"] for m in msgs if m["event"] == "delta" and m.get("stream_id") == "sa")
        sb = "".join(m["text"] for m in msgs if m["event"] == "delta" and m.get("stream_id") == "sb")
        assert sa == "A1A2"
        assert sb == "B1B2"


# -- Multi-client -------------------------------------------------------------


def test_independent_sessions(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    client = _build(ch)

    with _connect(client, ch, client_id="u1") as ws1:
        r1 = _recv(ws1)
        with _connect(client, ch, client_id="u2") as ws2:
            r2 = _recv(ws2)
            asyncio.run(ch.send(OutboundMessage(
                channel="websocket", chat_id=r1["chat_id"], content="for-u1",
            )))
            assert _recv(ws1)["text"] == "for-u1"
            asyncio.run(ch.send(OutboundMessage(
                channel="websocket", chat_id=r2["chat_id"], content="for-u2",
            )))
            assert _recv(ws2)["text"] == "for-u2"


def test_disconnected_client_cleanup(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    client = _build(ch)

    with _connect(client, ch, client_id="tmp") as ws:
        chat_id = _recv(ws)["chat_id"]
    # disconnected — _subs must be clean after the context manager exits
    asyncio.run(ch.send(OutboundMessage(
        channel="websocket", chat_id=chat_id, content="orphan",
    )))
    assert chat_id not in ch._subs


# -- Authentication -----------------------------------------------------------


def test_static_token_accepted(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch, token="secret")
    client = _build(ch)

    with _connect(client, ch, client_id="a", token="secret") as ws:
        r = _recv(ws)
        assert r["event"] == "ready"
        assert r["client_id"] == "a"


def test_static_token_rejected(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch, token="correct")
    client = _build(ch, raise_server_exceptions=False)

    with pytest.raises(Exception):
        with _connect(client, ch, client_id="b", token="wrong") as ws:
            ws.receive_text()


def test_websocket_requires_token_rejected_without_token(tmp_path, monkeypatch):
    """When websocketRequiresToken=True and no token is supplied, the connection
    must be rejected.  A bootstrap token is the supported way to authenticate."""
    ch = _make_channel(tmp_path, monkeypatch, websocketRequiresToken=True)
    client = _build(ch, raise_server_exceptions=False)

    with pytest.raises(Exception):
        with _connect(client, ch, client_id="x") as ws:
            ws.receive_text()


def test_websocket_requires_token_accepted_with_bootstrap_token(tmp_path, monkeypatch):
    """A bootstrap token (from /webui/bootstrap) is accepted as a WS connection
    token when websocketRequiresToken=True."""
    ch = _make_channel(tmp_path, monkeypatch, websocketRequiresToken=True)
    client = _build(ch)

    token = _bootstrap_token(client)
    with _connect(client, ch, client_id="ok", token=token) as ws:
        r = _recv(ws)
        assert r["event"] == "ready"
        assert r["client_id"] == "ok"


# -- Path routing -------------------------------------------------------------


def test_custom_path(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch, path="/my-chat")
    client = _build(ch)

    with client.websocket_connect("/my-chat?client_id=p") as ws:
        r = _recv(ws)
        assert r["event"] == "ready"


def test_wrong_path_rejected(tmp_path, monkeypatch):
    """Connecting to a path other than the configured WS path must be rejected."""
    ch = _make_channel(tmp_path, monkeypatch, path="/ws")
    client = _build(ch, raise_server_exceptions=False)

    with pytest.raises(Exception):
        with client.websocket_connect("/wrong") as ws:
            ws.receive_text()


# -- Edge cases ---------------------------------------------------------------


def test_large_message(tmp_path, monkeypatch):
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    ch = WebSocketChannel(cfg, bus)
    client = _build(ch)

    with _connect(client, ch, client_id="big") as ws:
        _recv(ws)  # ready
        big = "x" * 100_000
        ws.send_text(big)
        time.sleep(0.1)
        assert bus.publish_inbound.call_args[0][0].content == big


def test_unicode_roundtrip(tmp_path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    client = _build(ch)

    with _connect(client, ch, client_id="u") as ws:
        ready = _recv(ws)
        text = "你好世界 \U0001f30d 日本語テスト"
        ws.send_text(text)
        # Verify server received the unicode inbound (no direct bus mock here;
        # the channel will try to dispatch to its real bus which does nothing)
        asyncio.run(ch.send(OutboundMessage(
            channel="websocket", chat_id=ready["chat_id"], content=text,
        )))
        msg = _recv(ws)
        assert msg["text"] == text


def test_rapid_fire(tmp_path, monkeypatch):
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    ch = WebSocketChannel(cfg, bus)
    client = _build(ch)

    with _connect(client, ch, client_id="r") as ws:
        ready = _recv(ws)
        for i in range(50):
            ws.send_text(f"in-{i}")
        time.sleep(0.3)
        assert bus.publish_inbound.await_count == 50
        for i in range(50):
            asyncio.run(ch.send(OutboundMessage(
                channel="websocket", chat_id=ready["chat_id"], content=f"out-{i}",
            )))
        received = [_recv(ws)["text"] for _ in range(50)]
        assert received == [f"out-{i}" for i in range(50)]


def test_invalid_json_as_plain_text(tmp_path, monkeypatch):
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    ch = WebSocketChannel(cfg, bus)
    client = _build(ch)

    with _connect(client, ch, client_id="j") as ws:
        _recv(ws)  # ready
        ws.send_text("{broken json")
        time.sleep(0.05)
        assert bus.publish_inbound.call_args[0][0].content == "{broken json"
