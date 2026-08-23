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
    """Minimal fake web client: chat_postMessage (all a plain text send
    needs) plus chat_update (needed when an answer takes over a pending
    status message instead of posting fresh)."""

    def __init__(self, ts: str) -> None:
        self.ts = ts
        self.chat_post_calls: list[dict[str, object | None]] = []
        self.chat_update_calls: list[dict[str, object | None]] = []

    async def chat_postMessage(self, **kwargs):  # noqa: N802 - mirrors Slack SDK
        self.chat_post_calls.append(kwargs)
        return {"ok": True, "ts": self.ts}

    async def chat_update(self, **kwargs):  # noqa: N802 - mirrors Slack SDK
        self.chat_update_calls.append(kwargs)
        return {"ok": True, "ts": kwargs.get("ts")}


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


def _progress(chat_id: str, *, thread_ts: str | None = None) -> OutboundMessage:
    """A progress/status update — the shape that puts a status-only buffer
    into _stream_bufs, which a following real answer can then take over."""
    slack_meta: dict[str, object] = {}
    if thread_ts is not None:
        slack_meta["thread_ts"] = thread_ts
    return OutboundMessage(
        channel="slack",
        chat_id=chat_id,
        content="working on it",
        metadata={"_progress": True, "_tool_hint": True, "slack": slack_meta},
    )


@pytest.mark.asyncio
async def test_send_returns_receipt_when_answer_claims_status_message() -> None:
    """The common approval-ask shape: a status line was already showing, and
    the real (single-chunk) answer takes it over via chat_update instead of
    posting fresh. The receipt must still name that thread."""
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeReceiptClient(ts="1700000000.000100")
    channel._web_client = fake_web

    await channel.send(_progress("C123"))
    assert len(fake_web.chat_post_calls) == 1  # the status line was posted

    receipt = await channel.send(
        OutboundMessage(channel="slack", chat_id="C123", content="Approve X?")
    )

    assert fake_web.chat_update_calls  # delivered via edit, not a new post
    assert len(fake_web.chat_post_calls) == 1  # no second message was opened
    assert receipt == SendReceipt(thread_key="slack:C123:1700000000.000100")


@pytest.mark.asyncio
async def test_send_returns_existing_thread_receipt_when_answer_claims_status_message() -> None:
    """Same takeover, but the outbound already targets an existing thread:
    that thread's key wins over the status message's own ts."""
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeReceiptClient(ts="1700000000.000200")
    channel._web_client = fake_web

    await channel.send(_progress("C123", thread_ts="111.222"))
    assert len(fake_web.chat_post_calls) == 1

    receipt = await channel.send(
        OutboundMessage(
            channel="slack",
            chat_id="C123",
            content="Approve X?",
            metadata={"slack": {"thread_ts": "111.222"}},
        )
    )

    assert fake_web.chat_update_calls
    assert receipt == SendReceipt(thread_key="slack:C123:111.222")
