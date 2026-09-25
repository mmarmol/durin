"""One validated submission path for chat messages, shared by the WebSocket
``message`` frame and the HTTP send route."""

import pytest

from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel
from durin.service.types import ValidationFailedError


def _ch() -> tuple[WebSocketChannel, MessageBus]:
    bus = MessageBus()
    return WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus), bus


def test_validate_rejects_bad_chat_id() -> None:
    ch, _ = _ch()
    with pytest.raises(ValidationFailedError) as exc:
        ch.validate_chat_message("bad id!", "hi", None)
    assert exc.value.details == {"detail": "invalid chat_id"}


def test_validate_rejects_non_string_content() -> None:
    ch, _ = _ch()
    with pytest.raises(ValidationFailedError) as exc:
        ch.validate_chat_message("c1", None, None)
    assert exc.value.details == {"detail": "missing content"}


def test_validate_rejects_empty_message_without_media() -> None:
    ch, _ = _ch()
    with pytest.raises(ValidationFailedError) as exc:
        ch.validate_chat_message("c1", "   ", None)
    assert exc.value.details == {"detail": "missing content"}


def test_validate_rejects_malformed_media() -> None:
    ch, _ = _ch()
    with pytest.raises(ValidationFailedError) as exc:
        ch.validate_chat_message("c1", "hi", "not-a-list")
    assert exc.value.details == {"detail": "image_rejected", "reason": "malformed"}


def test_validate_returns_no_media_for_plain_text() -> None:
    ch, _ = _ch()
    assert ch.validate_chat_message("c1", "hi", None) == []


@pytest.mark.asyncio
async def test_publish_builds_the_same_inbound_as_the_websocket() -> None:
    ch, bus = _ch()
    await ch.publish_chat_message(
        sender_id="api:tok", chat_id="c1", content="hello", media_paths=[],
        webui=True, steer=True, client_msg_id="cm-1", origin="api",
    )
    msg = await bus.consume_inbound()
    assert (msg.channel, msg.chat_id, msg.sender_id, msg.content) == ("websocket", "c1", "api:tok", "hello")
    assert msg.metadata["webui"] is True
    assert msg.metadata["steer"] is True
    assert msg.metadata["client_msg_id"] == "cm-1"
    assert msg.metadata["origin"] == "api"


@pytest.mark.asyncio
async def test_publish_without_origin_leaves_it_out() -> None:
    ch, bus = _ch()
    await ch.publish_chat_message(sender_id="u", chat_id="c1", content="hi", media_paths=[])
    assert "origin" not in (await bus.consume_inbound()).metadata


@pytest.mark.asyncio
async def test_api_message_user_row_records_its_origin() -> None:
    from durin.utils.webui_transcript import get_transcript_writer, read_transcript_page

    ch, _ = _ch()
    await ch.publish_chat_message(
        sender_id="api:tok", chat_id="c-origin", content="from a script", media_paths=[],
        webui=True, origin="api",
    )
    await get_transcript_writer().flush("websocket:c-origin")
    rows, _ = read_transcript_page("websocket:c-origin")
    assert rows[0]["event"] == "user"
    assert rows[0]["origin"] == "api"


@pytest.mark.asyncio
async def test_websocket_frame_errors_keep_their_tokens() -> None:
    from unittest.mock import AsyncMock

    ch, _ = _ch()
    ws = AsyncMock()
    await ch._dispatch_envelope(ws, "client-1", {"type": "message", "chat_id": "c1", "content": "  "})
    import json

    frame = json.loads(ws.send_text.await_args.args[0])
    assert frame["event"] == "error"
    assert frame["detail"] == "missing content"
