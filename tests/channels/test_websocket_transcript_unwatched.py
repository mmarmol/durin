"""Output a turn produces while no client watches its chat still reaches the
display transcript, so a client that comes back sees what happened."""

from unittest.mock import MagicMock

import pytest

from durin.bus.events import OutboundMessage
from durin.channels.websocket import WebSocketChannel
from durin.utils.webui_transcript import get_transcript_writer, read_transcript_page


def _ch() -> WebSocketChannel:
    return WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())


async def _records(chat_id: str) -> list[dict]:
    await get_transcript_writer().flush(f"websocket:{chat_id}")
    rows, _ = read_transcript_page(f"websocket:{chat_id}")
    return rows


@pytest.mark.asyncio
async def test_reply_without_subscribers_is_recorded() -> None:
    channel = _ch()
    await channel.send(OutboundMessage(
        channel="websocket", chat_id="c-reply", content="all done", metadata={},
    ))
    rows = await _records("c-reply")
    assert [r["text"] for r in rows if r.get("event") == "message"] == ["all done"]


@pytest.mark.asyncio
async def test_stream_without_subscribers_is_recorded() -> None:
    channel = _ch()
    await channel.send_delta("c-delta", "Hel", {"_stream_delta": True, "_stream_id": "s1"})
    await channel.send_delta("c-delta", "", {"_stream_end": True, "_stream_id": "s1"})
    rows = await _records("c-delta")
    assert [r["event"] for r in rows] == ["delta", "stream_end"]


@pytest.mark.asyncio
async def test_turn_end_without_subscribers_is_recorded() -> None:
    channel = _ch()
    await channel.send_turn_end("c-end", latency_ms=12)
    rows = await _records("c-end")
    assert rows[-1]["event"] == "turn_end"


@pytest.mark.asyncio
async def test_reasoning_without_subscribers_is_recorded() -> None:
    channel = _ch()
    await channel.send_reasoning_delta("c-think", "hmm", {"_stream_id": "r1"})
    await channel.send_reasoning_end("c-think", {"_stream_id": "r1"})
    rows = await _records("c-think")
    assert [r["event"] for r in rows] == ["reasoning_delta", "reasoning_end"]


@pytest.mark.asyncio
async def test_live_state_without_subscribers_is_not_recorded() -> None:
    channel = _ch()
    await channel.send(OutboundMessage(
        channel="websocket", chat_id="c-live", content="",
        metadata={"_goal_status": True, "goal_status": "running"},
    ))
    assert await _records("c-live") == []
