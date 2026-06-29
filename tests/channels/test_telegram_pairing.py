"""Tests for Telegram pairing-code behaviour for unauthorized senders."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

try:
    import telegram  # noqa: F401
except ImportError:
    pytest.skip("Telegram dependencies not installed (python-telegram-bot)", allow_module_level=True)

from durin.bus.events import OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.telegram import TelegramChannel, TelegramConfig
from durin.pairing import PAIRING_CODE_META_KEY


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
async def test_deny_or_pair_dm_issues_pairing_code(monkeypatch) -> None:
    """Unauthorized sender in a DM receives a pairing code via send()."""
    channel = _make_channel(allow_from=[])
    sent: list[OutboundMessage] = []

    async def _fake_send(msg: OutboundMessage) -> None:
        sent.append(msg)

    monkeypatch.setattr(channel, "send", _fake_send)

    # Patch generate_code so we know what code to expect and avoid disk I/O
    with patch("durin.channels.base.generate_code", return_value="TESTCODE") as mock_gen:
        message = _make_message("private")
        await channel._deny_or_pair(message, "99|alice")

    mock_gen.assert_called_once_with("telegram", "99|alice")
    assert len(sent) == 1
    assert sent[0].metadata.get(PAIRING_CODE_META_KEY) == "TESTCODE"


@pytest.mark.asyncio
async def test_deny_or_pair_group_does_not_issue_pairing_code(monkeypatch) -> None:
    """Unauthorized sender in a group chat does NOT receive a pairing code."""
    channel = _make_channel(allow_from=[])
    sent: list[OutboundMessage] = []

    async def _fake_send(msg: OutboundMessage) -> None:
        sent.append(msg)

    monkeypatch.setattr(channel, "send", _fake_send)

    with patch("durin.channels.base.generate_code", return_value="TESTCODE") as mock_gen:
        message = _make_message("group")
        await channel._deny_or_pair(message, "99|alice")

    mock_gen.assert_not_called()
    assert len(sent) == 0
