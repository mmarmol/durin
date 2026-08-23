"""Tests for ChannelManager.send — the direct, receipt-returning send path.

Unlike publish_outbound (fire-and-forget through the bus queue, consumed
later by _dispatch_outbound), this is a synchronous passthrough for callers
that need the channel's SendReceipt back immediately.
"""

from __future__ import annotations

import pytest

from durin.bus.events import OutboundMessage, SendReceipt
from durin.bus.queue import MessageBus
from durin.channels.base import BaseChannel
from durin.channels.manager import ChannelManager
from durin.config.schema import Config


class _ReceiptChannel(BaseChannel):
    """Mock channel whose send() returns a deterministic SendReceipt."""

    name = "mock"
    display_name = "Mock"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: OutboundMessage) -> SendReceipt | None:
        return SendReceipt(thread_key=f"mock:{msg.chat_id}")


@pytest.fixture
def manager():
    bus = MessageBus()
    mgr = ChannelManager(Config(), bus)
    mgr.channels["mock"] = _ReceiptChannel({}, bus)
    return mgr


@pytest.mark.asyncio
async def test_manager_send_returns_channel_receipt(manager) -> None:
    receipt = await manager.send(
        OutboundMessage(channel="mock", chat_id="C1", content="hi")
    )

    assert receipt == SendReceipt(thread_key="mock:C1")


@pytest.mark.asyncio
async def test_manager_send_returns_none_for_unknown_channel(manager) -> None:
    receipt = await manager.send(
        OutboundMessage(channel="nope", chat_id="C1", content="hi")
    )

    assert receipt is None
