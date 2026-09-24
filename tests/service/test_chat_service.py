"""ChatService — send a message into a webui conversation, stop its turn."""

import pytest

from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel
from durin.service.chat import ChatSendCommand, ChatService, ChatStopCommand
from durin.service.principal import Principal
from durin.service.types import ForbiddenError, UnavailableError, ValidationFailedError

_WRITER = Principal.remote("tok1", frozenset({"chat:write"}))


def _svc(allow=("*",), stop_turn=None, turn_key=None):
    bus = MessageBus()
    ch = WebSocketChannel({"enabled": True, "allowFrom": list(allow)}, bus)
    return ChatService(channel_resolver=lambda: ch, stop_turn=stop_turn, turn_key=turn_key), bus


@pytest.mark.asyncio
async def test_send_publishes_a_webui_message_from_the_api() -> None:
    svc, bus = _svc()
    res = await svc.send(ChatSendCommand(key="websocket:c1", content="hi", client_msg_id="cm"), _WRITER)
    msg = await bus.consume_inbound()
    assert (msg.chat_id, msg.sender_id, msg.content) == ("c1", "api:tok1", "hi")
    assert msg.metadata["webui"] is True
    assert msg.metadata["origin"] == "api"
    assert msg.metadata["client_msg_id"] == "cm"
    assert (res.key, res.client_msg_id) == ("websocket:c1", "cm")


@pytest.mark.parametrize("key", ["slack:C1", "api:x", "websocket:", "websocket:bad id!"])
@pytest.mark.asyncio
async def test_send_rejects_keys_that_are_not_webui_conversations(key) -> None:
    svc, bus = _svc()
    with pytest.raises(ValidationFailedError):
        await svc.send(ChatSendCommand(key=key, content="hi"), _WRITER)
    assert bus.inbound_size == 0


@pytest.mark.asyncio
async def test_send_requires_chat_write() -> None:
    svc, _ = _svc()
    with pytest.raises(ForbiddenError):
        await svc.send(ChatSendCommand(key="websocket:c1", content="hi"),
                       Principal.remote("t", frozenset({"sessions:read"})))


@pytest.mark.asyncio
async def test_send_rejects_sender_not_allowed() -> None:
    svc, bus = _svc(allow=("someone-else",))
    with pytest.raises(ForbiddenError):
        await svc.send(ChatSendCommand(key="websocket:c1", content="hi"), _WRITER)
    assert bus.inbound_size == 0


@pytest.mark.asyncio
async def test_send_rejects_an_empty_message() -> None:
    svc, bus = _svc()
    with pytest.raises(ValidationFailedError):
        await svc.send(ChatSendCommand(key="websocket:c1", content="  "), _WRITER)
    assert bus.inbound_size == 0


@pytest.mark.asyncio
async def test_send_unavailable_without_the_chat_channel() -> None:
    svc = ChatService(channel_resolver=lambda: None)
    with pytest.raises(UnavailableError):
        await svc.send(ChatSendCommand(key="websocket:c1", content="hi"), _WRITER)


def test_media_items_reject_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ChatSendCommand.model_validate(
            {"key": "websocket:c1", "content": "", "media": [{"data_url": "data:x", "surprise": 1}]}
        )


@pytest.mark.asyncio
async def test_stop_maps_to_the_bus_turn_key() -> None:
    seen = []

    async def _stop(key: str) -> int:
        seen.append(key)
        return 1

    svc, _ = _svc(stop_turn=_stop, turn_key=lambda k: "unified:default")
    res = await svc.stop(ChatStopCommand(key="websocket:c1"), _WRITER)
    assert (res.stopped, seen) == (1, ["unified:default"])


@pytest.mark.asyncio
async def test_stop_rejects_other_channels() -> None:
    async def _stop(key: str) -> int:
        return 0

    svc, _ = _svc(stop_turn=_stop, turn_key=lambda k: k)
    with pytest.raises(ValidationFailedError):
        await svc.stop(ChatStopCommand(key="slack:C1"), _WRITER)


@pytest.mark.asyncio
async def test_stop_unavailable_without_a_loop() -> None:
    svc, _ = _svc()
    with pytest.raises(UnavailableError):
        await svc.stop(ChatStopCommand(key="websocket:c1"), _WRITER)


@pytest.mark.asyncio
async def test_send_mints_a_client_msg_id_when_absent() -> None:
    # It keys the live echo of the message and the turn_end that answers it.
    svc, bus = _svc()
    res = await svc.send(ChatSendCommand(key="websocket:c1", content="hi"), _WRITER)
    msg = await bus.consume_inbound()
    assert res.client_msg_id
    assert msg.metadata["client_msg_id"] == res.client_msg_id
