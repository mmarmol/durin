"""The ``turn_end`` frame says how the turn ended and which message opened it."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.bus.events import OutboundMessage
from durin.channels.websocket import WebSocketChannel


def _ch_with_watcher() -> tuple[WebSocketChannel, AsyncMock]:
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())
    ws = AsyncMock()
    channel._attach(ws, "c1")
    return channel, ws


@pytest.mark.asyncio
async def test_turn_end_frame_carries_outcome_and_client_msg_id() -> None:
    channel, ws = _ch_with_watcher()
    await channel.send(OutboundMessage(
        channel="websocket", chat_id="c1", content="",
        metadata={"_turn_end": True, "outcome": "stopped", "client_msg_id": "cm-9"},
    ))
    frame = json.loads(ws.send_text.await_args.args[0])
    assert frame == {
        "event": "turn_end", "chat_id": "c1", "outcome": "stopped", "client_msg_id": "cm-9",
    }


@pytest.mark.asyncio
async def test_turn_end_frame_ignores_unknown_outcome() -> None:
    channel, ws = _ch_with_watcher()
    await channel.send(OutboundMessage(
        channel="websocket", chat_id="c1", content="",
        metadata={"_turn_end": True, "outcome": "exploded"},
    ))
    assert "outcome" not in json.loads(ws.send_text.await_args.args[0])
