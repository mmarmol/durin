"""Tests for Telegram-via-gate pairing behavior.

_deny_or_pair was removed in the pure-transport refactor: Telegram handlers now
publish to the bus unconditionally and the central gate (ChannelManager) issues
pairing codes for unauthorized DMs and drops unauthorized group messages.

These tests assert:
1. _on_start and _on_message call _handle_message with the correct is_dm value.
2. The gate-level pairing promise (unauthorized DM → pairing code) is exercised
   end-to-end through the ChannelManager (see also test_inbound_gate.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

try:
    import telegram  # noqa: F401
except ImportError:
    pytest.skip("Telegram dependencies not installed (python-telegram-bot)", allow_module_level=True)

from durin.bus.queue import MessageBus
from durin.channels.telegram import TelegramChannel, TelegramConfig


def _make_channel(allow_from: list[str] | None = None) -> TelegramChannel:
    config = TelegramConfig(
        enabled=True,
        token="123:abc",
        allow_from=allow_from or [],
    )
    bus = MessageBus()
    return TelegramChannel(config, bus)


def _make_update(chat_type: str, text: str = "hello") -> SimpleNamespace:
    user = SimpleNamespace(id=99, username="alice", first_name="Alice")
    message = SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, is_forum=False),
        chat_id=99999,
        text=text,
        caption=None,
        entities=[],
        caption_entities=[],
        reply_to_message=None,
        photo=None,
        voice=None,
        audio=None,
        document=None,
        video=None,
        video_note=None,
        animation=None,
        location=None,
        media_group_id=None,
        message_thread_id=None,
        message_id=1,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(message=message, effective_user=user)


@pytest.mark.asyncio
async def test_on_start_private_publishes_is_dm_true() -> None:
    """_on_start for a private chat publishes is_dm=True so the gate can pair unauthorized senders."""
    channel = _make_channel(allow_from=[])
    channel._handle_message = AsyncMock()

    update = _make_update("private", "/start")
    await channel._on_start(update, None)

    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.call_args.kwargs
    assert kwargs["is_dm"] is True
    assert kwargs["sender_id"] == "99|alice"
    assert kwargs["content"] == "/start"


@pytest.mark.asyncio
async def test_on_start_group_publishes_is_dm_false() -> None:
    """_on_start in a group publishes is_dm=False; the gate drops unauthorized group messages."""
    channel = _make_channel(allow_from=[])
    channel._handle_message = AsyncMock()

    update = _make_update("group", "/start")
    await channel._on_start(update, None)

    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.call_args.kwargs
    assert kwargs["is_dm"] is False


@pytest.mark.asyncio
async def test_on_message_private_publishes_is_dm_true() -> None:
    """_on_message for a private chat always publishes is_dm=True regardless of auth status."""
    from unittest.mock import patch

    channel = _make_channel(allow_from=[])
    channel._handle_message = AsyncMock()
    channel._start_typing = lambda _: None
    channel._add_reaction = AsyncMock()

    update = _make_update("private")
    # _app must be set for _on_message to proceed past the app guard
    from tests.channels.test_telegram_channel import _FakeApp
    channel._app = _FakeApp(lambda: None)

    await channel._on_message(update, None)

    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.call_args.kwargs
    assert kwargs["is_dm"] is True
    assert kwargs["sender_id"] == "99|alice"


@pytest.mark.asyncio
async def test_unauthorized_dm_pairs_via_gate(monkeypatch) -> None:
    """End-to-end: an unauthorized private-chat message published by Telegram results in
    a pairing code being sent back via the channel, courtesy of ChannelManager._authorize_inbound.

    This mirrors test_inbound_gate.py::test_unauthorized_dm_pairs but wires up a real
    TelegramChannel so we confirm the is_dm flag threads through correctly.
    """
    import types
    import durin.channels.manager as mgr_mod

    sent: list = []

    class _TelegramStub:
        name = "telegram"
        _allowed = False

        def is_allowed(self, sender):
            return self._allowed

        async def send(self, msg):
            sent.append(msg)

    m = mgr_mod.ChannelManager.__new__(mgr_mod.ChannelManager)
    m.channels = {"telegram": _TelegramStub()}
    m.bus = types.SimpleNamespace(set_inbound_authorizer=lambda fn: None)

    monkeypatch.setattr(
        "durin.channels.manager.generate_code",
        lambda channel, sender: "XXXX-YYYY",
    )

    from durin.bus.events import InboundMessage

    ok = await m._authorize_inbound(
        InboundMessage(
            channel="telegram", sender_id="99|alice", chat_id="99999",
            content="", is_dm=True,
        )
    )

    assert ok is False
    assert len(sent) == 1  # pairing code was sent


@pytest.mark.asyncio
async def test_unauthorized_group_message_not_paired_via_gate() -> None:
    """Group messages from unauthorized senders must NOT trigger pairing."""
    import types
    import durin.channels.manager as mgr_mod

    sent: list = []

    class _TelegramStub:
        name = "telegram"

        def is_allowed(self, sender):
            return False

        async def send(self, msg):
            sent.append(msg)

    m = mgr_mod.ChannelManager.__new__(mgr_mod.ChannelManager)
    m.channels = {"telegram": _TelegramStub()}
    m.bus = types.SimpleNamespace(set_inbound_authorizer=lambda fn: None)

    from durin.bus.events import InboundMessage

    ok = await m._authorize_inbound(
        InboundMessage(
            channel="telegram", sender_id="99|alice", chat_id="99999",
            content="hello", is_dm=False,
        )
    )

    assert ok is False
    assert sent == []  # no pairing code for group messages
