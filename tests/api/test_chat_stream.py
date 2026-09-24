"""SSE transport for webui conversations: the subscriber and its routes."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.api import chat_stream
from durin.api.chat_stream import SseSubscriber
from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel
from durin.service.chat import ChatService
from durin.service.principal import Principal
from durin.service.registry import ServiceRegistry


def _frame(event: str, **fields) -> str:
    return json.dumps({"event": event, "chat_id": "c1", **fields})


def _chunk_len(raw: str) -> int:
    return len(f"event: {json.loads(raw)['event']}\ndata: {raw}\n\n".encode())


async def _take(sub: SseSubscriber, n: int) -> list[bytes]:
    out = []
    async for chunk in sub.frames():
        out.append(chunk)
        if len(out) == n:
            break
    return out


def _names(chunks: list[bytes]) -> list[bytes]:
    return [c.split(b"\n")[0] for c in chunks]


# -- the subscriber ----------------------------------------------------------


@pytest.mark.asyncio
async def test_frames_are_named_sse_events_carrying_the_websocket_json() -> None:
    sub = SseSubscriber()
    raw = _frame("delta", text="Hi")
    await sub.send_text(raw)
    [chunk] = await _take(sub, 1)
    assert chunk == f"event: delta\ndata: {raw}\n\n".encode()


@pytest.mark.asyncio
async def test_voice_frames_are_filtered() -> None:
    sub = SseSubscriber()
    await sub.send_text(_frame("voice_audio", data="xx"))
    await sub.send_text(_frame("turn_end"))
    assert _names(await _take(sub, 1)) == [b"event: turn_end"]


@pytest.mark.asyncio
async def test_keepalive_while_idle(monkeypatch) -> None:
    monkeypatch.setattr(chat_stream, "SSE_KEEPALIVE_S", 0.02)
    sub = SseSubscriber()
    assert await _take(sub, 1) == [b": keepalive\n\n"]


@pytest.mark.asyncio
async def test_full_buffer_drops_text_fragments_first() -> None:
    message, delta, end = _frame("message", text="x" * 100), _frame("delta", text="y" * 100), _frame("turn_end")
    sub = SseSubscriber(limit_bytes=_chunk_len(message) + _chunk_len(end))
    await sub.send_text(message)
    await sub.send_text(delta)  # does not fit: a fragment, dropped
    await sub.send_text(end)    # fits exactly
    assert _names(await _take(sub, 2)) == [b"event: message", b"event: turn_end"]


@pytest.mark.asyncio
async def test_full_buffer_ends_with_lagged_when_a_state_frame_cannot_fit() -> None:
    first, second = _frame("message", text="x" * 100), _frame("message", text="z" * 100)
    sub = SseSubscriber(limit_bytes=_chunk_len(first) + _chunk_len(second) - 1)
    await sub.send_text(first)
    await sub.send_text(second)              # a state frame that does not fit
    await sub.send_text(_frame("turn_end"))  # after lagged: ignored
    chunks = [c async for c in sub.frames()]
    assert _names(chunks) == [b"event: message", b"event: lagged"]


@pytest.mark.asyncio
async def test_draining_frees_room_in_the_buffer() -> None:
    raw = _frame("message", text="x" * 100)
    sub = SseSubscriber(limit_bytes=_chunk_len(raw))
    await sub.send_text(raw)
    await _take(sub, 1)
    await sub.send_text(raw)  # fits again once the first was yielded
    assert _names(await _take(sub, 1)) == [b"event: message"]


@pytest.mark.asyncio
async def test_slow_subscriber_does_not_block_other_watchers() -> None:
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())
    slow = SseSubscriber(limit_bytes=64)  # never drained
    fast = AsyncMock()
    channel._attach(slow, "c1")
    channel._attach(fast, "c1")
    for _ in range(50):
        await asyncio.wait_for(channel.send_delta("c1", "abcdef" * 10, {"_stream_id": "s"}), 1)
    assert fast.send_text.await_count == 50


# -- the routes --------------------------------------------------------------

_READER = Principal.remote("r", frozenset({"sessions:read", "chat:write"}))


def _routes(principal, *, allow=("*",)):
    bus = MessageBus()
    channel = WebSocketChannel({"enabled": True, "allowFrom": list(allow)}, bus)
    registry = ServiceRegistry()
    registry.register("chat", ChatService(channel_resolver=lambda: channel))
    routes = chat_stream.build_chat_stream_routes(channel, registry, resolve_principal=lambda _h: principal)
    by = {(r.path, tuple(sorted(r.methods))): r.endpoint for r in routes}
    events = by[("/api/v1/sessions/{key}/events", ("GET", "HEAD"))]
    send = by[("/api/v1/sessions/{key}/messages", ("POST",))]
    return channel, bus, events, send


def _req(key, *, body=None, accept="text/event-stream", length=None):
    raw = json.dumps(body or {}).encode()

    class _R:
        path_params = {"key": key}
        headers = {
            "accept": accept,
            "content-type": "application/json",
            "content-length": str(len(raw) if length is None else length),
        }
        client = None

        async def body(self):
            return raw

    return _R()


async def _drain(resp) -> list[bytes]:
    return [c async for c in resp.body_iterator]


@pytest.mark.asyncio
async def test_events_requires_a_token() -> None:
    _, _, events, send = _routes(None)
    assert (await events(_req("websocket:c1"))).status_code == 401
    assert (await send(_req("websocket:c1", body={"content": "hi"}))).status_code == 401


@pytest.mark.asyncio
async def test_events_requires_sessions_read() -> None:
    _, _, events, _ = _routes(Principal.remote("w", frozenset({"chat:write"})))
    assert (await events(_req("websocket:c1"))).status_code == 403


@pytest.mark.asyncio
async def test_events_rejects_non_webui_key() -> None:
    _, _, events, _ = _routes(_READER)
    assert (await events(_req("slack:C1"))).status_code == 422


@pytest.mark.asyncio
async def test_events_stream_carries_channel_frames_and_detaches_on_close() -> None:
    channel, _, events, _ = _routes(_READER)
    resp = await events(_req("websocket:c1"))
    body = resp.body_iterator
    first = asyncio.create_task(body.__anext__())
    await asyncio.sleep(0.01)
    await channel.send_delta("c1", "Hi", {"_stream_id": "s"})
    assert (await asyncio.wait_for(first, 1)).startswith(b"event: delta\n")
    await body.aclose()
    assert "c1" not in channel._subs


@pytest.mark.asyncio
async def test_events_open_replays_a_turn_in_flight(monkeypatch) -> None:
    import time

    from durin.utils import webui_turn_helpers

    channel, _, events, _ = _routes(_READER)
    monkeypatch.setitem(webui_turn_helpers._WEBSOCKET_TURN_WALL_STARTED_AT, "c1", time.time())
    resp = await events(_req("websocket:c1"))
    first = await asyncio.wait_for(resp.body_iterator.__anext__(), 1)
    await resp.body_iterator.aclose()
    assert first.startswith(b"event: goal_status\n")
    assert b'"running"' in first


@pytest.mark.asyncio
async def test_plain_send_answers_202() -> None:
    _, bus, _, send = _routes(_READER)
    resp = await send(_req("websocket:c1", body={"content": "hi"}, accept="application/json"))
    assert resp.status_code == 202
    assert (await bus.consume_inbound()).content == "hi"


@pytest.mark.asyncio
async def test_send_validation_errors_use_the_api_422_shape() -> None:
    _, _, _, send = _routes(_READER)
    resp = await send(_req("websocket:c1", body={"content": "hi", "surprise": 1}, accept="application/json"))
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")


@pytest.mark.asyncio
async def test_send_refuses_an_oversized_body() -> None:
    _, bus, _, send = _routes(_READER)
    resp = await send(_req("websocket:c1", body={"content": "hi"}, length=10**9))
    assert resp.status_code == 413
    assert bus.inbound_size == 0


@pytest.mark.asyncio
async def test_streaming_send_refuses_commands() -> None:
    _, bus, _, send = _routes(_READER)
    resp = await send(_req("websocket:c1", body={"content": "/status"}))
    assert resp.status_code == 422
    assert bus.inbound_size == 0


@pytest.mark.asyncio
async def test_streaming_send_needs_sessions_read_too() -> None:
    _, _, _, send = _routes(Principal.remote("w", frozenset({"chat:write"})))
    assert (await send(_req("websocket:c1", body={"content": "hi"}))).status_code == 403


@pytest.mark.asyncio
async def test_streaming_send_delivers_before_streaming_and_ends_on_its_turn() -> None:
    channel, bus, _, send = _routes(_READER)
    resp = await send(_req("websocket:c1", body={"content": "hi", "client_msg_id": "cm-1"}))
    # Delivered before the stream is read: nothing is lost if the client leaves now.
    inbound = await asyncio.wait_for(bus.consume_inbound(), 1)
    assert inbound.metadata["client_msg_id"] == "cm-1"
    reader = asyncio.create_task(_drain(resp))
    await asyncio.sleep(0.01)
    await channel.send_turn_end("c1", outcome="completed", client_msg_id="earlier")
    await channel.send_turn_end("c1", outcome="completed", client_msg_id="cm-1")
    chunks = await asyncio.wait_for(reader, 1)
    assert _names(chunks)[-2:] == [b"event: turn_end", b"event: turn_end"]
    assert b'"cm-1"' in chunks[-1]
    assert "c1" not in channel._subs


@pytest.mark.asyncio
async def test_streaming_send_ends_on_the_turn_that_consumed_it_without_a_queued_notice() -> None:
    # The websocket channel's progress setting can drop message_queued;
    # queued_consumed is never gated.
    channel, bus, _, send = _routes(_READER)
    resp = await send(_req("websocket:c1", body={"content": "hi", "client_msg_id": "cm-2"}))
    await bus.consume_inbound()
    reader = asyncio.create_task(_drain(resp))
    await asyncio.sleep(0.01)
    await channel.send_queued_consumed("c1", ["cm-2"])
    await channel.send_turn_end("c1", outcome="completed", client_msg_id="opener")
    await asyncio.wait_for(reader, 1)


@pytest.mark.asyncio
async def test_streaming_send_mints_a_client_msg_id_when_absent() -> None:
    _, bus, _, send = _routes(_READER)
    resp = await send(_req("websocket:c1", body={"content": "hi"}))
    inbound = await bus.consume_inbound()
    assert inbound.metadata["client_msg_id"]
    await resp.body_iterator.aclose()


# -- mounted in the gateway app ----------------------------------------------


def test_gateway_app_routes_send_and_events(tmp_path, monkeypatch) -> None:
    from starlette.testclient import TestClient

    from durin.api.asgi import build_gateway_http_app
    from durin.security.api_tokens import ApiTokenStore

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    bus = MessageBus()
    channel = WebSocketChannel({
        "enabled": True, "allowFrom": ["*"], "host": "127.0.0.1", "port": 8765, "path": "/",
    }, bus)
    app = build_gateway_http_app(channel, channel._services, auth=channel._services.get("auth"))
    _tid, token = ApiTokenStore().issue(["chat:write", "sessions:read"], label="t")
    client = TestClient(app)
    hdr = {"Authorization": f"Bearer {token}"}

    r = client.post("/api/v1/sessions/websocket:c9/messages", json={"content": "hello"}, headers=hdr)
    assert r.status_code == 202
    assert r.json()["key"] == "websocket:c9"
    assert r.json()["client_msg_id"]
    assert bus.inbound_size == 1

    r = client.get("/api/v1/sessions/slack:C1/events", headers=hdr)
    assert r.status_code == 422
