"""Tests for SlackChannel.send returning a SendReceipt for the thread it used."""

from __future__ import annotations

import pytest

# Check optional Slack dependencies before running tests
try:
    import slack_sdk  # noqa: F401
except ImportError:
    pytest.skip("Slack dependencies not installed (slack-sdk)", allow_module_level=True)

from durin.bus.events import OutboundMessage, SendReceipt
from durin.bus.queue import MessageBus
from durin.channels.slack import SlackChannel, SlackConfig


class _FakeReceiptClient:
    """Minimal fake web client: only chat_postMessage, which is all a plain
    text send needs."""

    def __init__(self, ts: str) -> None:
        self.ts = ts
        self.chat_post_calls: list[dict[str, object | None]] = []

    async def chat_postMessage(self, **kwargs):  # noqa: N802 - mirrors Slack SDK
        self.chat_post_calls.append(kwargs)
        return {"ok": True, "ts": self.ts}


@pytest.mark.asyncio
async def test_send_returns_receipt_with_new_message_ts_as_thread_key() -> None:
    """A plain send with no existing thread opens one: the receipt carries
    the freshly-posted message's own ts as the new thread's key."""
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeReceiptClient(ts="1724400000.000100")
    channel._web_client = fake_web

    receipt = await channel.send(
        OutboundMessage(channel="slack", chat_id="C123", content="hello")
    )

    assert receipt == SendReceipt(thread_key="slack:C123:1724400000.000100")


@pytest.mark.asyncio
async def test_send_returns_receipt_with_existing_thread_key() -> None:
    """When the outbound already targets an existing thread (thread_ts set,
    origin chat matches), the receipt carries THAT thread's key instead of
    the freshly-posted message's own ts."""
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeReceiptClient(ts="999999.000999")
    channel._web_client = fake_web

    receipt = await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="hello",
            metadata={"slack": {"thread_ts": "111.222"}},
        )
    )

    assert receipt == SendReceipt(thread_key="slack:C123:111.222")


@pytest.mark.asyncio
async def test_send_returns_none_for_media_only_message() -> None:
    """No chunk is ever posted for a pure-media send with no text content,
    so there is no ts to build a thread key from."""
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeReceiptClient(ts="1724400000.000100")
    channel._web_client = fake_web

    receipt = await channel.send(
        OutboundMessage(channel="slack", chat_id="C123", content="", media=["/tmp/demo.txt"])
    )

    assert receipt is None
    assert fake_web.chat_post_calls == []
