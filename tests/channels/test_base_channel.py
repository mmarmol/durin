from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from durin.bus.events import InboundMessage, OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.base import BaseChannel


class _DummyChannel(BaseChannel):
    name = "dummy"
    _sent: list[OutboundMessage]

    def __init__(self, config, bus):
        super().__init__(config, bus)
        self._sent = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self._sent.append(msg)


def test_is_allowed_requires_exact_match() -> None:
    channel = _DummyChannel(SimpleNamespace(allow_from=["allow@email.com"]), MessageBus())

    assert channel.is_allowed("allow@email.com") is True
    assert channel.is_allowed("attacker|allow@email.com") is False


def test_is_allowed_supports_dict_allow_from_alias() -> None:
    channel = _DummyChannel({"allowFrom": ["alice"]}, MessageBus())

    assert channel.is_allowed("alice") is True


def test_is_allowed_denies_empty_dict_allow_from() -> None:
    channel = _DummyChannel({"allow_from": []}, MessageBus())

    assert channel.is_allowed("alice") is False


def test_is_allowed_handles_none_allow_from() -> None:
    channel = _DummyChannel({"allow_from": None}, MessageBus())
    assert channel.is_allowed("alice") is False

    channel2 = _DummyChannel({"allowFrom": None}, MessageBus())
    assert channel2.is_allowed("alice") is False


def test_is_allowed_star_allows_all() -> None:
    channel = _DummyChannel({"allowFrom": ["*"]}, MessageBus())
    assert channel.is_allowed("anyone") is True


def test_is_allowed_pairing_fallback(monkeypatch) -> None:
    channel = _DummyChannel({"allowFrom": []}, MessageBus())
    monkeypatch.setattr(
        "durin.channels.base.is_approved", lambda _ch, sid: sid == "paired"
    )
    assert channel.is_allowed("paired") is True
    assert channel.is_allowed("unknown") is False


@pytest.mark.asyncio
async def test_handle_message_publishes_inbound_dm() -> None:
    """_handle_message always publishes to the bus regardless of sender auth.

    Authorization + pairing are the gate's responsibility (ChannelManager).
    The base only builds and publishes the InboundMessage, carrying is_dm so
    the gate can decide whether to issue a pairing code.
    """
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    channel = _DummyChannel({"allowFrom": []}, bus)

    await channel._handle_message(
        sender_id="stranger", chat_id="chat1", content="hello", is_dm=True
    )

    bus.publish_inbound.assert_awaited_once()
    published: InboundMessage = bus.publish_inbound.call_args[0][0]
    assert published.sender_id == "stranger"
    assert published.chat_id == "chat1"
    assert published.is_dm is True
    # Base must NOT send anything itself — pairing is the gate's job
    assert channel._sent == []


@pytest.mark.asyncio
async def test_handle_message_publishes_inbound_group() -> None:
    """_handle_message publishes group messages too; the gate decides to drop them."""
    bus = MessageBus()
    bus.publish_inbound = AsyncMock()
    channel = _DummyChannel({"allowFrom": []}, bus)

    await channel._handle_message(
        sender_id="stranger", chat_id="chat1", content="hello", is_dm=False
    )

    bus.publish_inbound.assert_awaited_once()
    published: InboundMessage = bus.publish_inbound.call_args[0][0]
    assert published.is_dm is False
    assert channel._sent == []

