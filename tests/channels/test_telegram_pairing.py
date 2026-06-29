"""Tests for TelegramChannel._deny_or_pair routing."""

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


def _make_message(chat_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, is_forum=False),
        chat_id=99999,
    )


@pytest.mark.asyncio
async def test_deny_or_pair_dm_calls_handle_message_with_is_dm_true() -> None:
    """_deny_or_pair forwards private-chat messages with is_dm=True.

    The gate (ChannelManager._authorize_inbound) receives the InboundMessage and
    issues the pairing code — tested in test_inbound_gate.py::test_unauthorized_dm_pairs.
    """
    channel = _make_channel(allow_from=[])
    channel._handle_message = AsyncMock()

    message = _make_message("private")
    await channel._deny_or_pair(message, "99|alice")

    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.call_args.kwargs
    assert kwargs["is_dm"] is True
    assert kwargs["sender_id"] == "99|alice"


@pytest.mark.asyncio
async def test_deny_or_pair_group_calls_handle_message_with_is_dm_false() -> None:
    """_deny_or_pair forwards group-chat messages with is_dm=False.

    The gate drops the message silently without sending a pairing code —
    tested in test_inbound_gate.py::test_unauthorized_group_denied.
    """
    channel = _make_channel(allow_from=[])
    channel._handle_message = AsyncMock()

    message = _make_message("group")
    await channel._deny_or_pair(message, "99|alice")

    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.call_args.kwargs
    assert kwargs["is_dm"] is False
