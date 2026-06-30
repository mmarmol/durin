"""A non-streamed `message` frame carries a stable server id.

Command outputs (e.g. /persona) emit no turn_end, so the live row is never
re-anchored to a persisted id; a later refetch wholesale-replaces the message
array and the live row "disappears" then "reappears". The fix stamps a stable
``id`` on the SAME payload that is both sent on the wire and persisted to the
webui transcript, so live and replay rows share a React key and merge.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.bus.events import OutboundMessage
from durin.channels.websocket import WebSocketChannel
from durin.utils.webui_transcript import read_transcript_lines


def _ch() -> WebSocketChannel:
    return WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())


@pytest.mark.asyncio
async def test_message_frame_has_stable_id() -> None:
    """The wire `message` frame for a command output carries a non-empty id."""
    channel = _ch()
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(
        OutboundMessage(
            channel="websocket",
            chat_id="chat-1",
            content="## Persona",
            metadata={"render_as": "text"},
        )
    )

    payload = json.loads(mock_ws.send_text.await_args.args[0])
    assert payload["event"] == "message"
    assert isinstance(payload.get("id"), str) and payload["id"]


@pytest.mark.asyncio
async def test_message_frame_id_matches_persisted_record() -> None:
    """The id on the wire is identical to the id persisted in the transcript."""
    channel = _ch()
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(
        OutboundMessage(
            channel="websocket",
            chat_id="chat-1",
            content="## Persona",
            metadata={"render_as": "text"},
        )
    )

    wire = json.loads(mock_ws.send_text.await_args.args[0])
    persisted = read_transcript_lines("websocket:chat-1")
    msg_records = [r for r in persisted if r.get("event") == "message"]
    assert msg_records, "command output should be persisted"
    assert msg_records[-1].get("id") == wire["id"]
