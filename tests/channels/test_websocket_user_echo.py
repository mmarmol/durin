"""A user message reaches every watcher of the conversation live, so a
conversation driven from the API (or another tab) shows the question, not only
the agent's answer."""

import json
from unittest.mock import AsyncMock

import pytest

from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel
from durin.utils.webui_transcript import replay_transcript_to_ui_messages


def _ch() -> tuple[WebSocketChannel, AsyncMock]:
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, MessageBus())
    watcher = AsyncMock()
    channel._attach(watcher, "c1")
    return channel, watcher


def _frames(watcher: AsyncMock) -> list[dict]:
    return [json.loads(call.args[0]) for call in watcher.send_text.await_args_list]


@pytest.mark.asyncio
async def test_a_message_is_echoed_to_the_conversation_s_watchers() -> None:
    channel, watcher = _ch()
    await channel.publish_chat_message(
        sender_id="api:t", chat_id="c1", content="what's up?", media_paths=[],
        webui=True, client_msg_id="cm-7", origin="api",
    )
    [frame] = [f for f in _frames(watcher) if f["event"] == "user"]
    assert frame == {
        "event": "user", "chat_id": "c1", "text": "what's up?",
        "client_msg_id": "cm-7", "origin": "api",
    }


@pytest.mark.asyncio
async def test_a_message_without_a_client_msg_id_is_not_echoed() -> None:
    # The webui sends /stop without one: there is no row to reconcile it with.
    channel, watcher = _ch()
    await channel.publish_chat_message(
        sender_id="u", chat_id="c1", content="/stop", media_paths=[], webui=True,
    )
    assert [f for f in _frames(watcher) if f["event"] == "user"] == []


def test_replay_keeps_a_message_s_origin() -> None:
    rows = [
        {"event": "user", "chat_id": "c1", "text": "from a script", "origin": "api"},
        {"event": "user", "chat_id": "c1", "text": "from a person"},
    ]
    users = [m for m in replay_transcript_to_ui_messages(rows) if m["role"] == "user"]
    assert [m.get("origin") for m in users] == ["api", None]
