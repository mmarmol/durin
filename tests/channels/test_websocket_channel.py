"""Unit and lightweight integration tests for the WebSocket channel."""

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from durin.api.asgi import build_gateway_http_app
from durin.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.websocket import (
    WebSocketChannel,
    WebSocketConfig,
    _is_valid_chat_id,
    _issue_route_secret_matches,
    _normalize_config_path,
    _parse_envelope,
    _parse_inbound_payload,
    publish_dream_progress,
    publish_runtime_model_update,
)
from durin.config.loader import load_config, save_config
from durin.config.schema import Config

# -- Shared helpers (aligned with test_websocket_integration.py) ---------------

_PORT = 29876


def _ch(bus: Any, **kw: Any) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": _PORT,
        "path": "/ws",
        "websocketRequiresToken": False,
    }
    cfg.update(kw)
    return WebSocketChannel(cfg, bus)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


def _build_client(
    bus: Any,
    monkeypatch: Any,
    tmp_path: Any,
    **channel_kw: Any,
) -> tuple[WebSocketChannel, TestClient]:
    """Build a WebSocketChannel + unified TestClient for socket-based tests.

    Any keyword argument accepted by ``_ch`` can be passed to override the
    default channel config (e.g. ``allowFrom``, ``token``, ``websocketRequiresToken``).
    The data dir is isolated to ``tmp_path`` so tests never touch ``~/.durin``.
    """
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    channel = _ch(bus, **channel_kw)
    registry = channel._services
    auth = registry.get("auth")
    app = build_gateway_http_app(channel, registry, auth=auth)
    client = TestClient(app, raise_server_exceptions=False)
    return channel, client


def test_normalize_config_path_matches_request() -> None:
    assert _normalize_config_path("/ws/") == "/ws"
    assert _normalize_config_path("/") == "/"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ('{"content": "hi"}', "hi"),
        ('{"text": "there"}', "there"),
        ('{"message": "x"}', "x"),
        ("  ", None),
        ("{}", None),
    ],
)
def test_parse_inbound_payload(raw: str, expected: str | None) -> None:
    assert _parse_inbound_payload(raw) == expected


def test_parse_inbound_invalid_json_falls_back_to_raw_string() -> None:
    assert _parse_inbound_payload("{not json") == "{not json"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"content": ""}', None),           # empty string content
        ('{"content": 123}', None),          # non-string content
        ('{"content": "  "}', None),         # whitespace-only content
        ('["hello"]', '["hello"]'),           # JSON array: not a dict, treated as plain text
        ('{"unknown_key": "val"}', None),    # unrecognized key
        ('{"content": null}', None),         # null content
    ],
)
def test_parse_inbound_payload_edge_cases(raw: str, expected: str | None) -> None:
    assert _parse_inbound_payload(raw) == expected


def test_web_socket_config_path_must_start_with_slash() -> None:
    with pytest.raises(ValueError, match='path must start with "/"'):
        WebSocketConfig(path="bad")


def test_ssl_context_requires_both_cert_and_key_files() -> None:
    bus = MagicMock()
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"], "sslCertfile": "/tmp/c.pem", "sslKeyfile": ""},
        bus,
    )
    with pytest.raises(ValueError, match="ssl_certfile and ssl_keyfile"):
        channel._build_ssl_context()


def test_default_config_includes_safe_bind_and_streaming() -> None:
    defaults = WebSocketChannel.default_config()
    assert defaults["enabled"] is False
    assert defaults["host"] == "127.0.0.1"
    assert defaults["streaming"] is True
    assert defaults["allow_from"] == ["*"]


def test_issue_route_secret_matches_bearer_and_header() -> None:
    from websockets.datastructures import Headers

    secret = "my-secret"
    bearer_headers = Headers([("Authorization", "Bearer my-secret")])
    assert _issue_route_secret_matches(bearer_headers, secret) is True
    x_headers = Headers([("X-Durin-Auth", "my-secret")])
    assert _issue_route_secret_matches(x_headers, secret) is True
    wrong = Headers([("Authorization", "Bearer other")])
    assert _issue_route_secret_matches(wrong, secret) is False


def test_issue_route_secret_matches_empty_secret() -> None:
    from websockets.datastructures import Headers

    # Empty secret always returns True regardless of headers
    assert _issue_route_secret_matches(Headers([]), "") is True
    assert _issue_route_secret_matches(Headers([("Authorization", "Bearer anything")]), "") is True


@pytest.mark.asyncio
async def test_webui_message_envelope_marks_inbound_metadata(bus: MagicMock) -> None:
    channel = _ch(bus)
    conn = MagicMock()
    conn.remote_address = ("127.0.0.1", 50123)

    await channel._dispatch_envelope(
        conn,
        "webui-client",
        {"type": "message", "chat_id": "chat-1", "content": "hello", "webui": True},
    )

    msg = bus.publish_inbound.await_args.args[0]
    assert msg.channel == "websocket"
    assert msg.chat_id == "chat-1"
    assert msg.metadata["webui"] is True
    assert msg.metadata["_wants_stream"] is True


@pytest.mark.asyncio
async def test_plain_websocket_message_does_not_mark_webui(bus: MagicMock) -> None:
    channel = _ch(bus)
    conn = MagicMock()

    await channel._dispatch_envelope(
        conn,
        "custom-client",
        {"type": "message", "chat_id": "chat-1", "content": "hello"},
    )

    msg = bus.publish_inbound.await_args.args[0]
    assert "webui" not in msg.metadata


@pytest.mark.asyncio
async def test_steer_envelope_marks_metadata_and_client_msg_id(bus: MagicMock) -> None:
    channel = _ch(bus)
    conn = MagicMock()

    await channel._dispatch_envelope(
        conn,
        "webui-client",
        {
            "type": "message", "chat_id": "chat-1", "content": "focus on tests",
            "webui": True, "steer": True, "client_msg_id": "cm-42",
        },
    )

    msg = bus.publish_inbound.await_args.args[0]
    assert msg.metadata["steer"] is True
    assert msg.metadata["client_msg_id"] == "cm-42"


@pytest.mark.asyncio
async def test_plain_message_has_no_steer_metadata(bus: MagicMock) -> None:
    channel = _ch(bus)
    conn = MagicMock()

    await channel._dispatch_envelope(
        conn,
        "webui-client",
        {"type": "message", "chat_id": "chat-1", "content": "hello", "webui": True},
    )

    msg = bus.publish_inbound.await_args.args[0]
    assert "steer" not in msg.metadata


@pytest.mark.asyncio
async def test_send_message_queued_ack_frame() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket", chat_id="chat-1", content="",
        metadata={"_message_queued": True, "client_msg_id": "cm-42"},
    ))

    payload = json.loads(mock_ws.send_text.call_args[0][0])
    assert payload == {
        "event": "message_queued", "chat_id": "chat-1", "client_msg_id": "cm-42",
    }


@pytest.mark.asyncio
async def test_send_queued_consumed_ack_frame() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket", chat_id="chat-1", content="",
        metadata={"_queued_consumed": True, "client_msg_ids": ["cm-1", "cm-2"]},
    ))

    payload = json.loads(mock_ws.send_text.call_args[0][0])
    assert payload == {
        "event": "queued_consumed", "chat_id": "chat-1",
        "client_msg_ids": ["cm-1", "cm-2"],
    }


@pytest.mark.asyncio
async def test_send_delivers_json_message_with_media_and_reply() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    msg = OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="hello",
        reply_to="m1",
        media=["/tmp/a.png"],
        buttons=[["Yes", "No"]],
    )
    await channel.send(msg)

    mock_ws.send_text.assert_awaited_once()
    payload = json.loads(mock_ws.send_text.call_args[0][0])
    assert payload["event"] == "message"
    assert payload["chat_id"] == "chat-1"
    assert payload["text"] == "hello"
    assert payload["reply_to"] == "m1"
    assert payload["media"] == ["/tmp/a.png"]


@pytest.mark.asyncio
async def test_send_broadcasts_runtime_model_updates() -> None:
    bus = MessageBus()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    publish_runtime_model_update(bus, "openai/gpt-4.1", "fast")
    await channel.send(bus.outbound.get_nowait())

    payload = json.loads(mock_ws.send_text.call_args[0][0])
    assert payload["event"] == "runtime_model_updated"
    assert payload["model_name"] == "openai/gpt-4.1"
    assert payload["model_preset"] == "fast"


@pytest.mark.asyncio
async def test_runtime_model_update_publisher_uses_websocket_outbound_event() -> None:
    bus = MessageBus()

    publish_runtime_model_update(
        bus,
        "openai/gpt-4.1",
        "fast",
    )

    event = bus.outbound.get_nowait()
    assert event.channel == "websocket"
    assert event.chat_id == "*"
    assert event.content == ""
    assert event.metadata == {
        "_runtime_model_updated": True,
        "model": "openai/gpt-4.1",
        "model_preset": "fast",
    }


@pytest.mark.asyncio
async def test_send_broadcasts_dream_progress_to_all_connections() -> None:
    bus = MessageBus()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    # Two connections on different chats — dream progress is global, so both
    # must receive it (it is not scoped to a single chat's subscribers).
    conn_a = AsyncMock()
    conn_b = AsyncMock()
    channel._attach(conn_a, "chat-1")
    channel._attach(conn_b, "chat-2")

    publish_dream_progress(bus, {
        "kind": "activity",
        "item": {
            "kind": "merged", "summary": "Merged place:y → place:x",
            "ref": "place:x", "ref_kind": "entity", "at_ms": 1,
        },
    })
    await channel.send(bus.outbound.get_nowait())

    for conn in (conn_a, conn_b):
        conn.send_text.assert_awaited_once()
        payload = json.loads(conn.send_text.call_args[0][0])
        assert payload["event"] == "dream_progress"
        assert payload["kind"] == "activity"
        assert payload["item"]["ref"] == "place:x"


@pytest.mark.asyncio
async def test_dream_progress_publisher_uses_global_outbound_event() -> None:
    bus = MessageBus()

    publish_dream_progress(bus, {"kind": "run_started"})

    event = bus.outbound.get_nowait()
    assert event.channel == "websocket"
    assert event.chat_id == "*"
    assert event.content == ""
    assert event.metadata == {
        "_dream_progress": True,
        "dream": {"kind": "run_started"},
    }


@pytest.mark.asyncio
async def test_send_stages_external_media_as_signed_url(monkeypatch, tmp_path) -> None:
    bus = MagicMock()
    media_root = tmp_path / "media"
    ws_media = media_root / "websocket"
    ws_media.mkdir(parents=True)
    external = tmp_path / "clip.mp4"
    external.write_bytes(b"video")

    def fake_media_dir(channel: str | None = None):
        return ws_media if channel == "websocket" else media_root

    monkeypatch.setattr("durin.channels.websocket.get_media_dir", fake_media_dir)
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(
        OutboundMessage(
            channel="websocket",
            chat_id="chat-1",
            content="video",
            media=[str(external)],
        )
    )

    payload = json.loads(mock_ws.send_text.call_args[0][0])
    assert payload["media"] == [str(external)]
    assert payload["media_urls"][0]["name"] == "clip.mp4"
    assert payload["media_urls"][0]["url"].startswith("/api/media/")
    assert any(p.name.endswith("-clip.mp4") for p in ws_media.iterdir())


@pytest.mark.asyncio
async def test_send_missing_connection_is_noop_without_error() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    msg = OutboundMessage(channel="websocket", chat_id="missing", content="x")
    await channel.send(msg)


@pytest.mark.asyncio
async def test_send_removes_connection_on_connection_closed() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    mock_ws.send_text.side_effect = ConnectionClosed(Close(1006, ""), Close(1006, ""), True)
    channel._attach(mock_ws, "chat-1")

    msg = OutboundMessage(channel="websocket", chat_id="chat-1", content="hello")
    await channel.send(msg)

    assert "chat-1" not in channel._subs
    assert mock_ws not in channel._conn_chats


@pytest.mark.asyncio
async def test_send_progress_includes_structured_tool_events() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content='search "hermes"',
        metadata={
            "_progress": True,
            "_tool_hint": True,
            "_tool_events": [
                {
                    "version": 1,
                    "phase": "start",
                    "call_id": "call-1",
                    "name": "web_search",
                    "arguments": {"query": "hermes", "count": 8},
                    "result": None,
                    "error": None,
                    "files": [],
                    "embeds": [],
                }
            ],
        },
    ))

    payload = json.loads(mock_ws.send_text.await_args.args[0])
    assert payload["event"] == "message"
    assert payload["kind"] == "tool_hint"
    assert payload["tool_events"] == [
        {
            "version": 1,
            "phase": "start",
            "call_id": "call-1",
            "name": "web_search",
            "arguments": {"query": "hermes", "count": 8},
            "result": None,
            "error": None,
            "files": [],
            "embeds": [],
        }
    ]


@pytest.mark.asyncio
async def test_send_message_propagates_render_as_text_metadata() -> None:
    """Slash-command output (/status, /memory, /sessions, …) carries
    ``metadata['render_as'] = 'text'`` so the receiver knows the body
    is pre-formatted plain text. The WS channel must forward that hint
    on the wire — otherwise the webui feeds it to its Markdown pipeline
    and the column layout collapses into a single line."""
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="📊 Tokens: 4610 in / 320 out\n📚 Context: 8k/202k",
        metadata={"render_as": "text"},
    ))

    payload = json.loads(mock_ws.send_text.await_args.args[0])
    assert payload["event"] == "message"
    assert payload["render_as"] == "text"


@pytest.mark.asyncio
async def test_send_message_without_render_as_omits_field() -> None:
    """A conversational reply (no ``render_as``) must NOT carry the field
    — receivers default to Markdown rendering when absent."""
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="hello",
        metadata={},
    ))

    payload = json.loads(mock_ws.send_text.await_args.args[0])
    assert payload["event"] == "message"
    assert "render_as" not in payload


@pytest.mark.asyncio
async def test_send_progress_includes_agent_ui_blob() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    blob = {
        "kind": "panel",
        "data": {"version": 1, "event": "tick", "id": "r1"},
    }
    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="progress · panel",
        metadata={"_progress": True, OUTBOUND_META_AGENT_UI: blob},
    ))

    payload = json.loads(mock_ws.send_text.await_args.args[0])
    assert payload["event"] == "message"
    assert payload["kind"] == "progress"
    assert payload["agent_ui"] == blob


@pytest.mark.asyncio
async def test_send_delta_removes_connection_on_connection_closed() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"], "streaming": True}, bus)
    mock_ws = AsyncMock()
    mock_ws.send_text.side_effect = ConnectionClosed(Close(1006, ""), Close(1006, ""), True)
    channel._attach(mock_ws, "chat-1")

    await channel.send_delta("chat-1", "chunk", {"_stream_delta": True, "_stream_id": "s1"})

    assert "chat-1" not in channel._subs
    assert mock_ws not in channel._conn_chats


@pytest.mark.asyncio
async def test_send_delta_emits_delta_and_stream_end() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"], "streaming": True}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send_delta("chat-1", "part", {"_stream_delta": True, "_stream_id": "sid"})
    await channel.send_delta("chat-1", "", {"_stream_end": True, "_stream_id": "sid"})

    assert mock_ws.send_text.await_count == 2
    first = json.loads(mock_ws.send_text.call_args_list[0][0][0])
    second = json.loads(mock_ws.send_text.call_args_list[1][0][0])
    assert first["event"] == "delta"
    assert first["chat_id"] == "chat-1"
    assert first["text"] == "part"
    assert first["stream_id"] == "sid"
    assert second["event"] == "stream_end"
    assert second["chat_id"] == "chat-1"
    assert second["stream_id"] == "sid"


@pytest.mark.asyncio
async def test_send_reasoning_delta_emits_streaming_frame() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send_reasoning_delta(
        "chat-1",
        "step-by-step thinking",
        {"_reasoning_delta": True, "_stream_id": "r1"},
    )

    mock_ws.send_text.assert_awaited_once()
    payload = json.loads(mock_ws.send_text.await_args.args[0])
    assert payload["event"] == "reasoning_delta"
    assert payload["chat_id"] == "chat-1"
    assert payload["text"] == "step-by-step thinking"
    assert payload["stream_id"] == "r1"


@pytest.mark.asyncio
async def test_send_reasoning_end_emits_close_frame() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send_reasoning_end("chat-1", {"_reasoning_end": True, "_stream_id": "r1"})

    payload = json.loads(mock_ws.send_text.await_args.args[0])
    assert payload == {"event": "reasoning_end", "chat_id": "chat-1", "stream_id": "r1"}


@pytest.mark.asyncio
async def test_send_reasoning_one_shot_expands_to_delta_plus_end() -> None:
    """``send_reasoning`` is back-compat for hooks that haven't migrated:
    the base implementation must produce one delta and one end so the
    WebUI sees the same shape either way."""
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send_reasoning(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="thinking",
        metadata={"_reasoning": True},
    ))

    assert mock_ws.send_text.await_count == 2
    first = json.loads(mock_ws.send_text.call_args_list[0][0][0])
    second = json.loads(mock_ws.send_text.call_args_list[1][0][0])
    assert first["event"] == "reasoning_delta"
    assert first["text"] == "thinking"
    assert second["event"] == "reasoning_end"


@pytest.mark.asyncio
async def test_send_reasoning_delta_drops_empty_chunks() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send_reasoning_delta("chat-1", "", {"_reasoning_delta": True})

    mock_ws.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_reasoning_without_subscribers_is_noop() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)

    await channel.send_reasoning_delta("unattached", "thinking", None)
    await channel.send_reasoning_end("unattached", None)
    # No subscribers, no exception, no send.


@pytest.mark.asyncio
async def test_send_turn_end_emits_turn_end_event() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="",
        metadata={"_turn_end": True},
    ))

    mock_ws.send_text.assert_awaited_once()
    body = json.loads(mock_ws.send_text.await_args.args[0])
    assert body == {"event": "turn_end", "chat_id": "chat-1"}


@pytest.mark.asyncio
async def test_send_turn_end_includes_latency_ms_when_present() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="",
        metadata={"_turn_end": True, "latency_ms": 1500},
    ))

    mock_ws.send_text.assert_awaited_once()
    body = json.loads(mock_ws.send_text.await_args.args[0])
    assert body == {"event": "turn_end", "chat_id": "chat-1", "latency_ms": 1500}


@pytest.mark.asyncio
async def test_send_turn_end_includes_goal_state_when_present() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    blob = {"active": True, "ui_summary": "Explore codebase"}
    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="",
        metadata={"_turn_end": True, "goal_state": blob},
    ))

    mock_ws.send_text.assert_awaited_once()
    body = json.loads(mock_ws.send_text.await_args.args[0])
    assert body == {"event": "turn_end", "chat_id": "chat-1", "goal_state": blob}


@pytest.mark.asyncio
async def test_send_goal_status_running_emits_event_with_started_at() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="",
        metadata={
            "_goal_status": True,
            "goal_status": "running",
            "started_at": 1_700_000_000.5,
        },
    ))

    mock_ws.send_text.assert_awaited_once()
    body = json.loads(mock_ws.send_text.await_args.args[0])
    assert body == {
        "event": "goal_status",
        "chat_id": "chat-1",
        "status": "running",
        "started_at": 1_700_000_000.5,
    }


@pytest.mark.asyncio
async def test_send_goal_status_idle_omits_started_at() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="",
        metadata={
            "_goal_status": True,
            "goal_status": "idle",
            "goal_started_at": 99.0,
        },
    ))

    mock_ws.send_text.assert_awaited_once()
    body = json.loads(mock_ws.send_text.await_args.args[0])
    assert body == {"event": "goal_status", "chat_id": "chat-1", "status": "idle"}


@pytest.mark.asyncio
async def test_send_goal_state_emits_blob_per_chat() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_a = AsyncMock()
    mock_b = AsyncMock()
    channel._attach(mock_a, "chat-a")
    channel._attach(mock_b, "chat-b")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-a",
        content="",
        metadata={
            "_goal_state_sync": True,
            "goal_state": {"active": True, "ui_summary": "A"},
        },
    ))

    mock_a.send_text.assert_awaited_once()
    mock_b.send_text.assert_not_called()
    body = json.loads(mock_a.send_text.await_args.args[0])
    assert body == {
        "event": "goal_state",
        "chat_id": "chat-a",
        "goal_state": {"active": True, "ui_summary": "A"},
    }


@pytest.mark.asyncio
async def test_maybe_push_active_goal_state_noop_without_session_manager() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")
    channel._session_manager = None
    await channel._maybe_push_active_goal_state("chat-1")
    mock_ws.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_push_active_goal_state_skips_when_no_goal_on_disk() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    sm = MagicMock()
    sm.read_session_file.return_value = None
    channel._session_manager = sm
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")
    await channel._maybe_push_active_goal_state("chat-1")
    mock_ws.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_push_active_goal_state_notifies_when_goal_active_on_disk() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    sm = MagicMock()
    sm.read_session_file.return_value = {
        "metadata": {
            "goal_state": {
                "status": "active",
                "objective": "finish docs",
                "ui_summary": "Docs",
            },
        },
        "messages": [],
    }
    channel._session_manager = sm
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")
    await channel._maybe_push_active_goal_state("chat-1")
    mock_ws.send_text.assert_awaited_once()
    body = json.loads(mock_ws.send_text.await_args.args[0])
    assert body["event"] == "goal_state"
    assert body["chat_id"] == "chat-1"
    assert body["goal_state"]["active"] is True
    assert body["goal_state"]["objective"] == "finish docs"
    assert body["goal_state"]["ui_summary"] == "Docs"


@pytest.mark.asyncio
async def test_maybe_push_turn_run_wall_clock_skips_when_no_active_turn() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")
    from durin.utils import webui_turn_helpers as wth

    wth._WEBSOCKET_TURN_WALL_STARTED_AT.clear()
    await channel._maybe_push_turn_run_wall_clock("chat-1")
    mock_ws.send_text.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_push_turn_run_wall_clock_replays_running() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")
    from durin.utils import webui_turn_helpers as wth

    wth._WEBSOCKET_TURN_WALL_STARTED_AT.clear()
    try:
        wth._WEBSOCKET_TURN_WALL_STARTED_AT["chat-1"] = 1_700_000_000.0
        await channel._maybe_push_turn_run_wall_clock("chat-1")
    finally:
        wth._WEBSOCKET_TURN_WALL_STARTED_AT.pop("chat-1", None)

    mock_ws.send_text.assert_awaited_once()
    body = json.loads(mock_ws.send_text.await_args.args[0])
    assert body == {
        "event": "goal_status",
        "chat_id": "chat-1",
        "status": "running",
        "started_at": 1_700_000_000.0,
    }


@pytest.mark.asyncio
async def test_send_session_updated_emits_session_updated_event() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="",
        metadata={"_session_updated": True},
    ))

    mock_ws.send_text.assert_awaited_once()
    body = json.loads(mock_ws.send_text.await_args.args[0])
    assert body == {"event": "session_updated", "chat_id": "chat-1"}


@pytest.mark.asyncio
async def test_send_non_connection_closed_exception_is_raised() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    mock_ws.send_text.side_effect = RuntimeError("unexpected")
    channel._attach(mock_ws, "chat-1")

    msg = OutboundMessage(channel="websocket", chat_id="chat-1", content="hello")
    with pytest.raises(RuntimeError, match="unexpected"):
        await channel.send(msg)


@pytest.mark.asyncio
async def test_send_delta_missing_connection_is_noop() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"], "streaming": True}, bus)
    # No exception, no error — just a no-op
    await channel.send_delta("nonexistent", "chunk", {"_stream_delta": True, "_stream_id": "s1"})


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    # stop() before start() should not raise
    await channel.stop()
    await channel.stop()


def test_end_to_end_client_receives_ready_and_agent_sees_inbound(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    channel, client = _build_client(bus, monkeypatch, tmp_path)
    captured: dict = {}

    with client.websocket_connect("/ws?client_id=tester") as ws:
        ready = json.loads(ws.receive_text())
        assert ready["event"] == "ready"
        assert ready["client_id"] == "tester"
        captured["chat_id"] = ready["chat_id"]

        ws.send_text(json.dumps({"content": "ping from client"}))
        ws.send_text("plain text frame")

    # After the WS context closes, the server handler has finished; all
    # publish_inbound calls are complete.
    bus.publish_inbound.assert_awaited()
    inbound = bus.publish_inbound.call_args_list[0][0][0]
    assert inbound.channel == "websocket"
    assert inbound.sender_id == "tester"
    assert inbound.chat_id == captured["chat_id"]
    assert inbound.content == "ping from client"

    assert bus.publish_inbound.await_count >= 2
    second = bus.publish_inbound.call_args_list[1][0][0]
    assert second.content == "plain text frame"


def test_token_rejects_handshake_when_mismatch(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    _, client = _build_client(bus, monkeypatch, tmp_path, path="/", token="secret")

    with pytest.raises(Exception):
        with client.websocket_connect("/?token=wrong") as ws:
            ws.receive_text()


def test_wrong_path_returns_404(bus: MagicMock, monkeypatch, tmp_path) -> None:
    _, client = _build_client(bus, monkeypatch, tmp_path)

    # /other is not the WS path (/ws) — the server returns 403/404/close before accept.
    with pytest.raises(Exception):
        with client.websocket_connect("/other") as ws:
            ws.receive_text()


def test_registry_discovers_websocket_channel() -> None:
    from durin.channels.registry import load_channel_class

    cls = load_channel_class("websocket")
    assert cls.name == "websocket"


def test_bootstrap_token_then_websocket_requires_it(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    channel, client = _build_client(
        bus,
        monkeypatch,
        tmp_path,
        websocketRequiresToken=True,
    )

    token = client.get("/webui/bootstrap").json()["token"]
    assert token.startswith("nbwt_")

    # No token -> handshake rejected.
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?client_id=x") as ws:
            ws.receive_text()

    with client.websocket_connect(f"/ws?token={token}&client_id=caller") as ws:
        ready = json.loads(ws.receive_text())
        assert ready["event"] == "ready"
        assert ready["client_id"] == "caller"

    # Token was single-use — second connect must be rejected.
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws?token={token}&client_id=caller") as ws:
            ws.receive_text()


def test_settings_api_returns_safe_subset_and_updates_whitelist(
    bus: MagicMock,
    monkeypatch,
    tmp_path,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "openai/gpt-4o"
    config.providers.openai.api_key = "secret-key"
    config.tools.web.search.provider = "brave"
    config.tools.web.search.api_key = "brave-secret"
    save_config(config, config_path)
    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)

    channel, client = _build_client(bus, monkeypatch, tmp_path)
    tok = client.get("/webui/bootstrap").json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}

    settings = client.get("/api/v1/settings", headers=hdr)
    assert settings.status_code == 200
    body = settings.json()
    assert body["agent"]["model"] == "openai/gpt-4o"
    assert body["agent"]["provider"] == "openai"
    providers = {provider["name"]: provider for provider in body["providers"]}
    assert providers["openai"]["configured"] is True
    assert providers["openai"]["api_key_hint"] == "secr••••-key"
    assert providers["openrouter"]["configured"] is False
    assert body["agent"]["has_api_key"] is True
    assert body["web_search"]["provider"] == "brave"
    assert body["web_search"]["api_key_hint"] == "brav••••cret"
    search_providers = {provider["name"]: provider for provider in body["web_search"]["providers"]}
    assert search_providers["duckduckgo"]["credential"] == "none"
    assert search_providers["searxng"]["credential"] == "base_url"
    assert "secret-key" not in settings.text
    assert "brave-secret" not in settings.text

    provider_updated = client.post(
        "/api/v1/settings/provider",
        headers=hdr,
        json={
            "provider": "openrouter",
            "api_key": "sk-or-test",
            "api_base": "https://openrouter.ai/api/v1",
        },
    )
    assert provider_updated.status_code == 200
    provider_body = provider_updated.json()
    assert provider_body["requires_restart"] is False
    provider_rows = {provider["name"]: provider for provider in provider_body["providers"]}
    assert provider_rows["openrouter"]["configured"] is True
    assert "sk-or-test" not in provider_updated.text

    updated = client.post(
        "/api/v1/settings",
        headers=hdr,
        json={"model": "openrouter/test", "provider": "openrouter"},
    )
    assert updated.status_code == 200
    assert updated.json()["requires_restart"] is False

    search_updated = client.post(
        "/api/v1/settings/web-search",
        headers=hdr,
        json={"provider": "searxng", "base_url": "https://search.example.com"},
    )
    assert search_updated.status_code == 200
    search_body = search_updated.json()
    assert search_body["requires_restart"] is False
    assert search_body["web_search"]["provider"] == "searxng"
    assert search_body["web_search"]["api_key_hint"] is None
    assert search_body["web_search"]["base_url"] == "https://search.example.com"

    saved = load_config(config_path)
    assert saved.agents.defaults.model == "openrouter/test"
    assert saved.agents.defaults.provider == "openrouter"
    # The dashboard stores the key in the secret store and keeps a
    # ${secret:} reference in config — never plaintext.
    from durin.security.secrets import SecretStore, is_secret_ref

    assert is_secret_ref(saved.providers.openrouter.api_key)
    store_entry = SecretStore(
        path=config_path.parent / "secrets.json"
    ).load().get("OPENROUTER_API_KEY")
    assert store_entry is not None and store_entry.value == "sk-or-test"
    assert saved.providers.openrouter.api_base == "https://openrouter.ai/api/v1"
    assert saved.tools.web.search.provider == "searxng"
    assert saved.tools.web.search.api_key == ""
    assert saved.tools.web.search.base_url == "https://search.example.com"


def test_secrets_api_crud(bus: MagicMock, monkeypatch, tmp_path) -> None:
    """`/api/secrets` list/set/delete — values never returned."""
    import asyncio

    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)

    channel, client = _build_client(bus, monkeypatch, tmp_path)
    tok = client.get("/webui/bootstrap").json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}

    # Empty to start.
    listed = client.get("/api/v1/secrets", headers=hdr)
    assert listed.status_code == 200
    assert listed.json()["secrets"] == []

    # Create — via the `secret_store` websocket envelope. The value
    # rides a JSON frame, never a URL query.
    class _Conn:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.remote_address = ("127.0.0.1", 0)

        async def send_text(self, raw: str) -> None:
            self.sent.append(raw)

    conn = _Conn()
    asyncio.run(
        channel._handle_secret_store_envelope(
            conn,
            "client-x",
            {
                "type": "secret_store",
                "request_id": "r1",
                "name": "ATLASSIAN_WORK",
                "service": "atlassian",
                "value": "tok-plaintext-secret",
                "scope": ["exec"],
            },
        )
    )
    acks = [json.loads(raw) for raw in conn.sent]
    assert any(a.get("event") == "secret_stored" and a.get("ok") for a in acks)

    listed = client.get("/api/v1/secrets", headers=hdr)
    rows = listed.json()["secrets"]
    assert len(rows) == 1 and rows[0]["name"] == "ATLASSIAN_WORK"
    assert rows[0]["service"] == "atlassian" and rows[0]["scope"] == ["exec"]
    # The value is never in the response.
    assert "tok-plaintext-secret" not in listed.text

    from durin.security.secrets import SecretStore

    entry = SecretStore(path=tmp_path / "secrets.json").load().get("ATLASSIAN_WORK")
    assert entry is not None and entry.value == "tok-plaintext-secret"

    # Metadata-only edit (no value) keeps the stored value.
    conn2 = _Conn()
    asyncio.run(
        channel._handle_secret_store_envelope(
            conn2,
            "client-x",
            {
                "type": "secret_store",
                "request_id": "r2",
                "name": "ATLASSIAN_WORK",
                "service": "atlassian",
                "scope": ["exec", "skill:*"],
            },
        )
    )
    entry = SecretStore(path=tmp_path / "secrets.json").load().get("ATLASSIAN_WORK")
    assert entry.value == "tok-plaintext-secret"
    assert entry.scope == ["exec", "skill:*"]

    # Delete.
    deleted = client.request(
        "DELETE", "/api/v1/secrets", headers=hdr, json={"name": "ATLASSIAN_WORK"}
    )
    assert deleted.status_code == 200
    listed = client.get("/api/v1/secrets", headers=hdr)
    assert listed.json()["secrets"] == []

    # Unauthorized without a token.
    assert client.get("/api/v1/secrets").status_code == 401


class _FakeConn:
    """Minimal connection stub for unit-testing frame handlers."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.remote_address = ("127.0.0.1", 0)

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def last_event(self, name: str) -> dict | None:
        for frame in reversed(self.sent):
            if frame.get("event") == name:
                return frame
        return None


@pytest.mark.asyncio
async def test_secret_store_valid_new_secret_emits_ok_and_agent_resume(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    """Behavior 1: valid new secret → secret_stored {ok: True, name} + agent-resume."""
    monkeypatch.setattr(
        "durin.config.loader._current_config_path", tmp_path / "config.json"
    )
    channel = _ch(bus)
    conn = _FakeConn()
    await channel._handle_secret_store_envelope(
        conn,
        "client-1",
        {
            "type": "secret_store",
            "request_id": "r1",
            "name": "MY_TOKEN",
            "service": "github",
            "value": "s3cr3t-value",
            "scope": ["exec"],
            "chat_id": "chat-abc",
        },
    )
    ok_frame = conn.last_event("secret_stored")
    assert ok_frame is not None
    assert ok_frame["ok"] is True
    assert ok_frame["name"] == "MY_TOKEN"
    assert ok_frame.get("request_id") == "r1"
    # Agent-resume: a message carrying the metadata (but never the value) is
    # posted to the chat so the agent can continue.
    bus.publish_inbound.assert_awaited()
    resume = bus.publish_inbound.call_args[0][0]
    assert resume.chat_id == "chat-abc"
    assert "MY_TOKEN" in resume.content
    assert "s3cr3t-value" not in resume.content


@pytest.mark.asyncio
async def test_secret_store_invalid_name_emits_fail(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    """Behavior 2: invalid name → {ok: False, detail: 'invalid secret name (use UPPER_SNAKE)'}."""
    monkeypatch.setattr(
        "durin.config.loader._current_config_path", tmp_path / "config.json"
    )
    channel = _ch(bus)
    conn = _FakeConn()
    await channel._handle_secret_store_envelope(
        conn,
        "client-1",
        {"name": "lower-case", "service": "github", "value": "x" * 12},
    )
    fail_frame = conn.last_event("secret_stored")
    assert fail_frame is not None
    assert fail_frame["ok"] is False
    assert fail_frame["detail"] == "invalid secret name (use UPPER_SNAKE)"


@pytest.mark.asyncio
async def test_secret_store_missing_service_emits_fail(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    """Behavior 3: missing service → {ok: False, detail: 'service is required'}."""
    monkeypatch.setattr(
        "durin.config.loader._current_config_path", tmp_path / "config.json"
    )
    channel = _ch(bus)
    conn = _FakeConn()
    await channel._handle_secret_store_envelope(
        conn,
        "client-1",
        {"name": "MY_TOKEN", "service": "", "value": "x" * 12},
    )
    fail_frame = conn.last_event("secret_stored")
    assert fail_frame is not None
    assert fail_frame["ok"] is False
    assert fail_frame["detail"] == "service is required"


@pytest.mark.asyncio
async def test_secret_store_empty_value_on_new_secret_emits_fail(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    """Behavior 4: empty value on new secret → {ok: False, detail: 'value is required for a new secret'}."""
    monkeypatch.setattr(
        "durin.config.loader._current_config_path", tmp_path / "config.json"
    )
    channel = _ch(bus)
    conn = _FakeConn()
    await channel._handle_secret_store_envelope(
        conn,
        "client-1",
        {"name": "BRAND_NEW", "service": "github", "value": ""},
    )
    fail_frame = conn.last_event("secret_stored")
    assert fail_frame is not None
    assert fail_frame["ok"] is False
    assert fail_frame["detail"] == "value is required for a new secret"


@pytest.mark.asyncio
async def test_secret_store_empty_value_on_existing_secret_keeps_credential(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    """Behavior 5: empty value on an existing secret → {ok: True}, credential unchanged.

    An empty value is a metadata-only edit — it must NEVER wipe the stored
    secret. Updated metadata (service/scope) is applied; the value survives.
    """
    monkeypatch.setattr(
        "durin.config.loader._current_config_path", tmp_path / "config.json"
    )
    channel = _ch(bus)
    conn = _FakeConn()
    await channel._handle_secret_store_envelope(
        conn,
        "client-1",
        {"name": "MY_TOKEN", "service": "github", "value": "keep-this-value", "scope": ["exec"]},
    )
    await channel._handle_secret_store_envelope(
        conn,
        "client-1",
        {"name": "MY_TOKEN", "service": "gitlab", "value": "", "scope": ["skill:deploy"]},
    )
    ok_frame = conn.last_event("secret_stored")
    assert ok_frame is not None
    assert ok_frame["ok"] is True
    from durin.security.secrets import get_secret_store

    entry = get_secret_store(reload=True).get("MY_TOKEN")
    assert entry.value == "keep-this-value"  # credential preserved
    assert entry.service == "gitlab"  # metadata updated
    assert entry.scope == ["skill:deploy"]


def test_config_api_get_and_set(bus: MagicMock, monkeypatch, tmp_path) -> None:
    """`/api/config` returns the effective config + schema; `/set` writes one key."""
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)

    channel, client = _build_client(bus, monkeypatch, tmp_path)
    tok = client.get("/webui/bootstrap").json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}

    got = client.get("/api/v1/config", headers=hdr)
    assert got.status_code == 200
    body = got.json()
    # v1 emits snake_case (model_dump without by_alias) → json_schema, not schema.
    assert "config" in body and "json_schema" in body
    assert body["config"]["agents"]["defaults"]["model"]  # full, defaults filled

    # Set one value (the value is a JSON-encoded string, decoded server-side).
    updated = client.post(
        "/api/v1/config",
        headers=hdr,
        json={"key": "agents.defaults.temperature", "value": "0.25"},
    )
    assert updated.status_code == 200
    assert load_config(config_path).agents.defaults.temperature == 0.25

    # A schema-invalid value is rejected without writing.
    bad = client.post(
        "/api/v1/config",
        headers=hdr,
        json={"key": "agents.defaults.maxTokens", "value": '"nope"'},
    )
    assert bad.status_code in {400, 422}
    assert load_config(config_path).agents.defaults.max_tokens == 8192  # unchanged

    # Token required.
    assert client.get("/api/v1/config").status_code == 401


def test_channels_api_lists_discovered_channels(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    """`GET /api/channels` lists channels with enabled state + cred field."""
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)

    channel, client = _build_client(bus, monkeypatch, tmp_path)
    tok = client.get("/webui/bootstrap").json()["token"]

    got = client.get("/api/v1/channels", headers={"Authorization": f"Bearer {tok}"})
    assert got.status_code == 200
    channels = {c["name"]: c for c in got.json()["channels"]}
    assert "telegram" in channels
    tg = channels["telegram"]
    assert tg["enabled"] is False
    assert "display_name" in tg and "credential_field" in tg

    assert client.get("/api/v1/channels").status_code == 401


def test_models_and_capabilities_api(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    """`/api/models` lists a catalog; `/api/model/capabilities` resolves caps."""
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)

    channel, client = _build_client(bus, monkeypatch, tmp_path)
    tok = client.get("/webui/bootstrap").json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}

    models = client.get("/api/v1/models?provider=zhipu", headers=hdr)
    assert models.status_code == 200
    body = models.json()
    assert "suggested" in body and "models" in body
    assert any("glm" in m for m in body["suggested"])  # curated zhipu shortlist

    caps = client.get(
        "/api/v1/model/capabilities?model=glm-5.1&provider=zhipu", headers=hdr
    )
    assert caps.status_code == 200
    cb = caps.json()
    assert cb["model"] == "glm-5.1"
    assert "supports_vision" in cb and "max_input_tokens" in cb

    assert client.get("/api/v1/models").status_code == 401


def test_commands_api_returns_slash_command_metadata(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    channel, client = _build_client(bus, monkeypatch, tmp_path)
    tok = client.get("/webui/bootstrap").json()["token"]

    denied = client.get("/api/v1/commands")
    assert denied.status_code == 401

    response = client.get("/api/v1/commands", headers={"Authorization": f"Bearer {tok}"})
    assert response.status_code == 200
    body = response.json()
    commands = {row["command"]: row for row in body["commands"]}
    assert commands["/new"]["title"] == "New chat"
    assert commands["/model"]["arg_hint"] == "[preset]"
    assert all("description" in row for row in body["commands"])


def test_settings_payload_normalizes_camel_case_provider(
    bus: MagicMock,
    monkeypatch,
    tmp_path,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.provider = "minimaxAnthropic"
    save_config(config, config_path)
    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)

    body = _ch(bus)._services.get("settings")._payload().model_dump()

    assert body["agent"]["provider"] == "minimax_anthropic"


def test_end_to_end_server_pushes_streaming_deltas_to_client(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    import asyncio

    channel, client = _build_client(bus, monkeypatch, tmp_path, streaming=True)

    with client.websocket_connect("/ws?client_id=stream-tester") as ws:
        ready = json.loads(ws.receive_text())
        chat_id = ready["chat_id"]

        async def _push() -> None:
            await channel.send_delta(chat_id, "Hello ", {"_stream_delta": True, "_stream_id": "s1"})
            await channel.send_delta(chat_id, "world", {"_stream_delta": True, "_stream_id": "s1"})
            await channel.send_delta(chat_id, "", {"_stream_end": True, "_stream_id": "s1"})
            await channel.send(OutboundMessage(
                channel="websocket",
                chat_id=chat_id,
                content="",
                metadata={"_turn_end": True},
            ))

        asyncio.run(_push())

        delta1 = json.loads(ws.receive_text())
        assert delta1["event"] == "delta"
        assert delta1["text"] == "Hello "
        assert delta1["stream_id"] == "s1"

        delta2 = json.loads(ws.receive_text())
        assert delta2["event"] == "delta"
        assert delta2["text"] == "world"
        assert delta2["stream_id"] == "s1"

        end = json.loads(ws.receive_text())
        assert end["event"] == "stream_end"
        assert end["stream_id"] == "s1"

        turn_end = json.loads(ws.receive_text())
        assert turn_end == {"event": "turn_end", "chat_id": chat_id}


def test_bootstrap_rejects_when_issued_tokens_at_capacity(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    channel, client = _build_client(bus, monkeypatch, tmp_path)

    # Fill the issued-token pool to capacity.
    channel._issued_tokens = {
        f"nbwt_fill_{i}": time.monotonic() + 300 for i in range(channel._MAX_ISSUED_TOKENS)
    }

    resp = client.get("/webui/bootstrap")
    assert resp.status_code == 429
    # problem+json (RFC-9457), not the old {error} shape.
    assert resp.json()["status"] == 429


def test_allow_from_rejects_unauthorized_client_id(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    _, client = _build_client(bus, monkeypatch, tmp_path, allowFrom=["alice", "bob"])

    # The ASGI WS endpoint enforces allowFrom via channel.is_allowed before
    # accepting. An unauthorized client_id must result in connection closure.
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?client_id=eve") as ws:
            ws.receive_text()


def test_client_id_truncation(bus: MagicMock, monkeypatch, tmp_path) -> None:
    _, client = _build_client(bus, monkeypatch, tmp_path)
    long_id = "x" * 200
    with client.websocket_connect(f"/ws?client_id={long_id}") as ws:
        ready = json.loads(ws.receive_text())
        assert ready["client_id"] == "x" * 128
        assert len(ready["client_id"]) == 128


def test_non_utf8_binary_frame_ignored(bus: MagicMock, monkeypatch, tmp_path) -> None:
    _, client = _build_client(bus, monkeypatch, tmp_path)
    with client.websocket_connect("/ws?client_id=bin-test") as ws:
        ws.receive_text()  # consume ready
        # Send non-UTF-8 bytes.
        ws.send_bytes(b"\xff\xfe\xfd")
    # publish_inbound should NOT have been called.
    bus.publish_inbound.assert_not_awaited()


def test_static_token_accepts_issued_token_as_fallback(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    channel, client = _build_client(
        bus,
        monkeypatch,
        tmp_path,
        token="static-secret",
    )

    # With a static token configured, bootstrap is gated on that secret.
    issued_token = client.get(
        "/webui/bootstrap", headers={"Authorization": "Bearer static-secret"}
    ).json()["token"]

    # The handshake accepts the issued token even though a static token is set.
    with client.websocket_connect(f"/ws?token={issued_token}&client_id=caller") as ws:
        ready = json.loads(ws.receive_text())
        assert ready["event"] == "ready"


def test_allow_from_empty_list_denies_all(bus: MagicMock, monkeypatch, tmp_path) -> None:
    _, client = _build_client(bus, monkeypatch, tmp_path, allowFrom=[])

    with pytest.raises(Exception):
        with client.websocket_connect("/ws?client_id=anyone") as ws:
            ws.receive_text()


def test_websocket_requires_token_without_issue_path(
    bus: MagicMock, monkeypatch, tmp_path
) -> None:
    """When websocket_requires_token is True but no token or issue path configured, all connections are rejected."""
    _, client = _build_client(bus, monkeypatch, tmp_path, websocketRequiresToken=True)

    # No token at all → rejected.
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?client_id=u") as ws:
            ws.receive_text()

    # Wrong token → rejected.
    with pytest.raises(Exception):
        with client.websocket_connect("/ws?client_id=u&token=wrong") as ws:
            ws.receive_text()


# -- Multi-chat multiplexing -------------------------------------------------
#
# The multiplex protocol lets one WS connection route N logical chats over
# typed envelopes (`new_chat` / `attach` / `message`). Legacy frames must keep
# working on the connection's default chat_id.


def test_multiplex_legacy_still_works(bus: MagicMock, monkeypatch, tmp_path) -> None:
    import asyncio

    channel, client = _build_client(bus, monkeypatch, tmp_path)

    with client.websocket_connect("/ws?client_id=legacy") as ws:
        ready = json.loads(ws.receive_text())
        default_chat = ready["chat_id"]

        # Plain text frame routes to default chat_id.
        ws.send_text("hello from legacy")
        ws.send_text(json.dumps({"content": "structured legacy"}))

        # Outbound still reaches the legacy client.
        asyncio.run(
            channel.send(OutboundMessage(channel="websocket", chat_id=default_chat, content="reply"))
        )
        reply = json.loads(ws.receive_text())
        assert reply["event"] == "message"
        assert reply["chat_id"] == default_chat
        assert reply["text"] == "reply"

    # After disconnect, inbound calls recorded.
    calls = [c[0][0] for c in bus.publish_inbound.call_args_list]
    assert calls[0].chat_id == default_chat
    assert calls[0].content == "hello from legacy"
    assert calls[1].content == "structured legacy"


def test_multiplex_new_chat_roundtrip(bus: MagicMock, monkeypatch, tmp_path) -> None:
    import asyncio

    channel, client = _build_client(bus, monkeypatch, tmp_path)

    with client.websocket_connect("/ws?client_id=mp") as ws:
        ready = json.loads(ws.receive_text())
        default_chat = ready["chat_id"]

        ws.send_text(json.dumps({"type": "new_chat"}))
        attached = json.loads(ws.receive_text())
        assert attached["event"] == "attached"
        new_chat = attached["chat_id"]
        assert new_chat and new_chat != default_chat

        # Send on the new chat via typed envelope.
        ws.send_text(json.dumps({"type": "message", "chat_id": new_chat, "content": "hi on new"}))

        # Server pushes a message back; chat_id must match.
        asyncio.run(
            channel.send(OutboundMessage(channel="websocket", chat_id=new_chat, content="ok"))
        )
        reply = json.loads(ws.receive_text())
        assert reply["event"] == "message"
        assert reply["chat_id"] == new_chat
        assert reply["text"] == "ok"

    # Inbound arrived on the new chat.
    inbound = bus.publish_inbound.call_args[0][0]
    assert inbound.chat_id == new_chat
    assert inbound.content == "hi on new"


def test_multiplex_two_chats_isolated(bus: MagicMock, monkeypatch, tmp_path) -> None:
    import asyncio

    channel, client = _build_client(bus, monkeypatch, tmp_path)

    with client.websocket_connect("/ws?client_id=two") as ws:
        ws.receive_text()  # ready

        ws.send_text(json.dumps({"type": "new_chat"}))
        chat_a = json.loads(ws.receive_text())["chat_id"]
        ws.send_text(json.dumps({"type": "new_chat"}))
        chat_b = json.loads(ws.receive_text())["chat_id"]
        assert chat_a != chat_b

        asyncio.run(
            channel.send(OutboundMessage(channel="websocket", chat_id=chat_a, content="for-A"))
        )
        msg_a = json.loads(ws.receive_text())
        assert msg_a["chat_id"] == chat_a
        assert msg_a["text"] == "for-A"

        asyncio.run(
            channel.send(OutboundMessage(channel="websocket", chat_id=chat_b, content="for-B"))
        )
        msg_b = json.loads(ws.receive_text())
        assert msg_b["chat_id"] == chat_b
        assert msg_b["text"] == "for-B"


def test_multiplex_invalid_frames_return_error(bus: MagicMock, monkeypatch, tmp_path) -> None:
    _, client = _build_client(bus, monkeypatch, tmp_path)

    with client.websocket_connect("/ws?client_id=bad") as ws:
        ws.receive_text()  # ready

        # attach with bad chat_id
        ws.send_text(json.dumps({"type": "attach", "chat_id": "has space"}))
        err1 = json.loads(ws.receive_text())
        assert err1["event"] == "error"

        # message with missing content
        ws.send_text(json.dumps({"type": "message", "chat_id": "abc", "content": ""}))
        err2 = json.loads(ws.receive_text())
        assert err2["event"] == "error"

        # unknown type
        ws.send_text(json.dumps({"type": "nope"}))
        err3 = json.loads(ws.receive_text())
        assert err3["event"] == "error"

        # Connection survives: legacy frame still works.
        ws.send_text("still-alive")

    bus.publish_inbound.assert_awaited()
    assert bus.publish_inbound.call_args[0][0].content == "still-alive"


def test_multiplex_cleanup_on_disconnect(bus: MagicMock, monkeypatch, tmp_path) -> None:
    channel, client = _build_client(bus, monkeypatch, tmp_path)
    captured: dict = {}

    with client.websocket_connect("/ws?client_id=dc") as ws:
        ready = json.loads(ws.receive_text())
        captured["default_chat"] = ready["chat_id"]
        ws.send_text(json.dumps({"type": "new_chat"}))
        captured["extra_chat"] = json.loads(ws.receive_text())["chat_id"]
        assert captured["default_chat"] in channel._subs
        assert captured["extra_chat"] in channel._subs

    # Client gone. Server-side tracking must be empty.
    assert captured["default_chat"] not in channel._subs
    assert captured["extra_chat"] not in channel._subs
    assert not channel._conn_chats
    assert not channel._conn_default


def test_parse_envelope_detects_typed_frames() -> None:
    assert _parse_envelope('{"type":"new_chat"}') == {"type": "new_chat"}
    env = _parse_envelope('{"type":"message","chat_id":"abc","content":"hi"}')
    assert env == {"type": "message", "chat_id": "abc", "content": "hi"}


def test_parse_envelope_rejects_legacy_and_garbage() -> None:
    # No `type` field → legacy, caller falls back to _parse_inbound_payload.
    assert _parse_envelope('{"content":"hi"}') is None
    assert _parse_envelope("plain text") is None
    assert _parse_envelope("{broken") is None
    assert _parse_envelope("[1,2,3]") is None
    # Non-string `type` is not a valid envelope.
    assert _parse_envelope('{"type":123}') is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc", True),
        ("a1b2_c:d-e", True),
        ("x" * 64, True),
        ("unified:default", True),
        ("", False),
        ("x" * 65, False),
        ("has space", False),
        ("a/b", False),
        ("a.b", False),
        (None, False),
        (123, False),
    ],
)
def test_is_valid_chat_id(value: Any, expected: bool) -> None:
    assert _is_valid_chat_id(value) is expected


def test_v1_webui_thread_returns_signed_json(tmp_path, monkeypatch) -> None:
    """GET /api/v1/sessions/{key}/webui-thread — the signed front-door route
    builds the persisted display thread (media URLs HMAC-signed by the channel)."""
    from durin.utils.webui_transcript import append_transcript_object

    bus = MagicMock()
    # _build_client patches get_data_dir; seed the transcript afterwards so it
    # lands in the same dir the route reads from.
    _channel, client = _build_client(bus, monkeypatch, tmp_path)
    key = "websocket:c1"
    append_transcript_object(key, {"event": "user", "chat_id": "c1", "text": "hi"})
    tok = client.get("/webui/bootstrap").json()["token"]
    resp = client.get(
        f"/api/v1/sessions/{key}/webui-thread",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["sessionKey"] == key
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "hi"
