"""Tests for _stream_id-scoped coalescing to prevent cross-stream content-bleed.

Two concurrent streams sharing the same (channel, chat_id) — e.g. Telegram forum
topics with distinct session_keys — interleave deltas in the outbound queue.
Without a _stream_id guard, _coalesce_stream_deltas merges them, blending text
from stream B into stream A's edit bubble.  These tests verify the guard is in
place.
"""
import asyncio

import pytest

from durin.bus.events import OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.manager import ChannelManager
from durin.config.schema import Config


@pytest.fixture
def bus():
    return MessageBus()


@pytest.fixture
def manager(bus):
    return ChannelManager(Config(), bus)


def delta(content: str, stream_id: str, chat_id: str = "chat1") -> OutboundMessage:
    return OutboundMessage(
        channel="mock",
        chat_id=chat_id,
        content=content,
        metadata={"_stream_delta": True, "_stream_id": stream_id},
    )


class TestStreamIdCoalescing:
    """_coalesce_stream_deltas must not merge deltas from different _stream_ids."""

    @pytest.mark.asyncio
    async def test_interleaved_streams_not_cross_merged(self, manager, bus):
        """Interleaved A,B,A,B deltas must not concatenate B text into A or vice-versa.

        Queue order: A1, B1, A2, B2 — all same (channel, chat_id).
        The coalescer should stop at B1 (different _stream_id) and return only A1
        merged with nothing (B1 goes to pending).  A2 and B2 stay in the queue.
        """
        for msg in [
            delta("hello", "A"),
            delta("world", "B"),
            delta(" there", "A"),
            delta(" you", "B"),
        ]:
            await bus.publish_outbound(msg)

        first = await bus.consume_outbound()
        merged, pending = manager._coalesce_stream_deltas(first)

        # Stream A's first delta must NOT contain any text from stream B.
        assert "world" not in merged.content
        assert " you" not in merged.content
        assert merged.metadata.get("_stream_id") == "A"

        # The first non-A delta must be returned as pending (not dropped).
        assert len(pending) == 1
        assert pending[0].metadata.get("_stream_id") == "B"
        assert pending[0].content == "world"

        # Remaining items still in queue.
        remaining = [await bus.consume_outbound(), await bus.consume_outbound()]
        ids = [m.metadata["_stream_id"] for m in remaining]
        assert ids == ["A", "B"]

    @pytest.mark.asyncio
    async def test_same_stream_id_still_merges(self, manager, bus):
        """Positive control: consecutive same-_stream_id deltas must still coalesce."""
        for msg in [
            delta("foo", "A"),
            delta("bar", "A"),
            delta("baz", "A"),
        ]:
            await bus.publish_outbound(msg)

        first = await bus.consume_outbound()
        merged, pending = manager._coalesce_stream_deltas(first)

        assert merged.content == "foobarbaz"
        assert merged.metadata.get("_stream_id") == "A"
        assert pending == []

    @pytest.mark.asyncio
    async def test_no_stream_id_still_merges(self, manager, bus):
        """Positive control: deltas without _stream_id must still coalesce (backward compat)."""
        for text in ["alpha", "beta", "gamma"]:
            await bus.publish_outbound(OutboundMessage(
                channel="mock",
                chat_id="chat1",
                content=text,
                metadata={"_stream_delta": True},
            ))

        first = await bus.consume_outbound()
        merged, pending = manager._coalesce_stream_deltas(first)

        assert merged.content == "alphabetagamma"
        assert pending == []
