"""A client that sends a message and disconnects at once must not lose it.

Processing a ``message`` frame pushes hydration frames back to the sender
before the message reaches the agent. When the socket is already gone the
Starlette adapter must report it the way the channel handles a gone
connection (``ConnectionClosed``) instead of an exception that aborts the
frame's processing."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from durin.api.asgi import StarletteConnectionAdapter
from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel


@pytest.mark.parametrize(
    "error",
    [WebSocketDisconnect(code=1006), RuntimeError('Cannot call "send" once a close message has been sent.')],
)
@pytest.mark.asyncio
async def test_send_on_a_gone_socket_reports_connection_closed(error) -> None:
    ws = MagicMock()
    ws.send_text = AsyncMock(side_effect=error)
    with pytest.raises(ConnectionClosed):
        await StarletteConnectionAdapter(ws).send_text("{}")


@pytest.mark.asyncio
async def test_message_from_a_client_that_already_left_still_reaches_the_agent() -> None:
    bus = MessageBus()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    # Hydration pushes a concurrency snapshot to the sender before publishing.
    channel._runtime_concurrency_snapshot = lambda: {"interactive": {"running": 0}}
    ws = MagicMock()
    ws.send_text = AsyncMock(side_effect=WebSocketDisconnect(code=1006))
    ws.client = None
    conn = StarletteConnectionAdapter(ws)

    await channel._dispatch_envelope(
        conn, "client-1", {"type": "message", "chat_id": "c1", "content": "hi"},
    )

    msg = await bus.consume_inbound()
    assert (msg.chat_id, msg.content) == ("c1", "hi")
    assert "c1" not in channel._subs
