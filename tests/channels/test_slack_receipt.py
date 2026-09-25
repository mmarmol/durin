"""Tests for SlackChannel.send returning a SendReceipt for the thread it used."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# Check optional Slack dependencies before running tests
try:
    import slack_sdk  # noqa: F401
except ImportError:
    pytest.skip("Slack dependencies not installed (slack-sdk)", allow_module_level=True)

from durin.agent.approval_prompt import make_chat_asker
from durin.bus.events import OUTBOUND_META_ASKS_PERSON, OutboundMessage, SendReceipt
from durin.bus.queue import MessageBus
from durin.channels.slack import SlackChannel, SlackConfig


class _FakeReceiptClient:
    """Minimal fake web client: chat_postMessage (all a plain text send
    needs), chat_update (needed when an answer takes over a pending status
    message instead of posting fresh) and chat_delete (a flagged question
    retires the status line it replaces)."""

    def __init__(self, ts: str) -> None:
        self.ts = ts
        self.chat_post_calls: list[dict[str, object | None]] = []
        self.chat_update_calls: list[dict[str, object | None]] = []
        self.chat_delete_calls: list[dict[str, object | None]] = []

    async def chat_postMessage(self, **kwargs):  # noqa: N802 - mirrors Slack SDK
        self.chat_post_calls.append(kwargs)
        return {"ok": True, "ts": self.ts}

    async def chat_update(self, **kwargs):  # noqa: N802 - mirrors Slack SDK
        self.chat_update_calls.append(kwargs)
        return {"ok": True, "ts": kwargs.get("ts")}

    async def chat_delete(self, **kwargs):  # noqa: N802 - mirrors Slack SDK
        self.chat_delete_calls.append(kwargs)
        return {"ok": True}


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


@pytest.mark.asyncio
async def test_flagged_question_posts_fresh_and_retires_the_status_line() -> None:
    """A question the turn blocks on must notify, so it cannot silently take
    over the status line via chat_update the way a plain answer does — it
    posts as its own message. The now-redundant status line is then deleted
    rather than left stranded above the question it was announcing progress
    towards. Progress and an unflagged answer that follow behave exactly as
    before: a new status line, then taken over in place."""
    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeReceiptClient(ts="1700000000.000300")
    channel._web_client = fake_web

    await channel.send(_progress("C123", thread_ts="200.000"))
    assert len(fake_web.chat_post_calls) == 1  # the status line was posted

    receipt = await channel.send(OutboundMessage(
        channel="slack", chat_id="C123", content="Approve X?",
        metadata={"slack": {"thread_ts": "200.000"}, OUTBOUND_META_ASKS_PERSON: True},
    ))

    assert fake_web.chat_update_calls == []  # never edited the status line in place
    assert len(fake_web.chat_post_calls) == 2  # status line + fresh question
    assert fake_web.chat_post_calls[1]["thread_ts"] == "200.000"
    assert fake_web.chat_post_calls[1]["text"] == "Approve X?"
    assert fake_web.chat_delete_calls == [
        {"channel": "C123", "ts": "1700000000.000300"}
    ]
    assert "C123" not in channel._stream_bufs
    assert receipt == SendReceipt(thread_key="slack:C123:200.000")

    # A following progress message opens a new status line...
    await channel.send(_progress("C123", thread_ts="200.000"))
    assert len(fake_web.chat_post_calls) == 3

    # ...and a following unflagged answer takes THAT one over as usual, so
    # the thread reads in order: question, the person's reply, the answer.
    await channel.send(OutboundMessage(
        channel="slack", chat_id="C123", content="Approved, done.",
        metadata={"slack": {"thread_ts": "200.000"}},
    ))
    assert fake_web.chat_update_calls  # took over the new status line
    assert len(fake_web.chat_post_calls) == 3  # no additional post


@pytest.mark.asyncio
async def test_a_slack_ask_lands_in_the_threads_own_conversation() -> None:
    """End-to-end through the real publish path: a chat asker's text copy,
    published on the bus, must land in the Slack thread the turn's mention
    came from — not at the channel's top level, where a reply would open a
    different conversation the waiting turn is not listening to."""
    from durin.agent import pending_answers as pa

    class _Sessions:
        def __init__(self) -> None:
            self.s = SimpleNamespace(metadata={})

        def get_or_create(self, key):
            return self.s

        def save(self, session, **kw):
            pass

    channel = SlackChannel(SlackConfig(enabled=True), MessageBus())
    fake_web = _FakeReceiptClient(ts="1700000000.000400")
    channel._web_client = fake_web

    class _ForwardingBus:
        async def publish_outbound(self, msg):
            await channel.send(msg)

    request_ctx = SimpleNamespace(
        session_key="slack:C123", channel="slack", chat_id="C123",
        metadata={"slack": {"thread_ts": "200.000", "event": {"channel": "C123"}}},
    )
    record = {"id": "r1", "kind": "exec_command", "summary": "run `rm -rf build`",
              "detail": {"command": "rm -rf build"}}

    pa.reset()
    pa.set_consumer_active(True)
    try:
        ask = make_chat_asker(
            sessions=_Sessions(), bus=_ForwardingBus(),
            request_ctx=request_ctx, timeout_s=0.05,
        )
        assert await ask(record) is None
    finally:
        pa.reset()

    assert fake_web.chat_post_calls
    call = fake_web.chat_post_calls[0]
    assert call["channel"] == "C123"
    assert call["thread_ts"] == "200.000"
