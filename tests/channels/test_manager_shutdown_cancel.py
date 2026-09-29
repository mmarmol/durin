"""Stopping the outbound dispatcher must finish even when a frame lands on
the bus at the moment it is cancelled.

The gateway's shutdown cancels the turns in flight before it stops the
channels; their ``finally`` publishes status frames, so the dispatcher's
``queue.get()`` completes in the same loop iterations in which ``stop_all``
cancels it. On Python 3.11 ``asyncio.wait_for`` returns the finished inner
result instead of raising the pending ``CancelledError`` in that window, the
dispatcher keeps looping with its one cancellation consumed, and
``await self._dispatch_task`` never returns — the gateway hung for good on
every SIGTERM with a turn in flight.
"""

from __future__ import annotations

import asyncio

import pytest

from durin.bus.events import OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.base import BaseChannel
from durin.channels.manager import ChannelManager
from durin.config.schema import Config


class _FakeChannel(BaseChannel):
    name = "websocket"

    def __init__(self) -> None:
        super().__init__(config={}, bus=MessageBus())
        self.sent: list[OutboundMessage] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


def _manager() -> tuple[ChannelManager, MessageBus, _FakeChannel]:
    bus = MessageBus()
    manager = ChannelManager(Config(), bus)
    channel = _FakeChannel()
    manager.channels["websocket"] = channel
    return manager, bus, channel


def _frame(text: str) -> OutboundMessage:
    return OutboundMessage(channel="websocket", chat_id="c1", content=text)


async def _stop_all_bounded(manager: ChannelManager) -> None:
    await asyncio.wait_for(manager.stop_all(), timeout=3)


@pytest.mark.asyncio
async def test_stop_all_finishes_when_a_frame_arrives_as_the_dispatcher_is_cancelled() -> None:
    manager, bus, channel = _manager()
    manager._dispatch_task = asyncio.create_task(manager._dispatch_outbound())
    await asyncio.sleep(0.05)  # the dispatcher is parked in its queue wait

    loop = asyncio.get_running_loop()
    bus.outbound.put_nowait(_frame("idle"))
    # Let the inner get() finish; the cancellation is then scheduled behind
    # the callback that releases the dispatcher's waiter and ahead of the
    # dispatcher's own resumption — the window the shutdown race hits.
    await asyncio.sleep(0)
    loop.call_soon(manager._dispatch_task.cancel)
    await asyncio.sleep(0)

    await _stop_all_bounded(manager)

    assert manager._dispatch_task.done()


@pytest.mark.asyncio
async def test_stop_all_finishes_when_frames_keep_arriving_while_it_cancels() -> None:
    """Stress the same window: a producer keeps publishing frames while
    stop_all cancels the dispatcher; every run must finish within the bound."""
    for _ in range(20):
        manager, bus, channel = _manager()
        manager._dispatch_task = asyncio.create_task(manager._dispatch_outbound())
        await asyncio.sleep(0.01)

        async def producer() -> None:
            i = 0
            while True:
                await bus.publish_outbound(_frame(str(i)))
                i += 1
                await asyncio.sleep(0)

        prod = asyncio.create_task(producer())
        await asyncio.sleep(0)
        await _stop_all_bounded(manager)
        prod.cancel()
        with pytest.raises(asyncio.CancelledError):
            await prod
        assert manager._dispatch_task.done()


@pytest.mark.asyncio
async def test_dispatcher_delivers_a_frame_taken_before_the_cancel() -> None:
    """Sanity: a frame the dispatcher already took is still delivered; the
    fix must not drop it, only stop afterwards."""
    manager, bus, channel = _manager()
    manager._dispatch_task = asyncio.create_task(manager._dispatch_outbound())
    await asyncio.sleep(0.05)
    await bus.publish_outbound(_frame("hello"))
    await asyncio.sleep(0.05)
    await _stop_all_bounded(manager)
    assert [m.content for m in channel.sent] == ["hello"]


class _StopProbeChannel(BaseChannel):
    """A channel whose stop() returns only once *release* is set."""

    def __init__(self, name: str, release: asyncio.Event) -> None:
        super().__init__(config={}, bus=MessageBus())
        self.name = name
        self.release = release
        self.stop_began = asyncio.Event()
        self.stopped = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stop_began.set()
        await self.release.wait()
        self.stopped = True

    async def send(self, msg: OutboundMessage) -> None:
        return None


@pytest.mark.asyncio
async def test_stop_all_stops_channels_concurrently() -> None:
    """Channels were stopped one after another, so the slowest one's stop
    (Slack's socket close took about five seconds) was added to every
    restart. Here the first channel's stop returns only once the second one
    has begun stopping, which a one-by-one stop never reaches."""
    manager = ChannelManager(Config(), MessageBus())
    second = _StopProbeChannel("second", asyncio.Event())
    second.release.set()
    first = _StopProbeChannel("first", second.stop_began)
    manager.channels = {"first": first, "second": second}

    async with asyncio.timeout(3):
        await manager.stop_all()

    assert first.stopped and second.stopped


@pytest.mark.asyncio
async def test_a_channel_that_never_stops_does_not_hold_up_the_others(monkeypatch) -> None:
    """Each channel's stop is bounded: one that hangs is logged by name and
    left behind, the others still stop, and stop_all returns."""
    from loguru import logger

    monkeypatch.setattr("durin.channels.manager._CHANNEL_STOP_TIMEOUT_S", 0.1, raising=False)
    manager = ChannelManager(Config(), MessageBus())
    hung = _StopProbeChannel("hung", asyncio.Event())
    quick = _StopProbeChannel("quick", asyncio.Event())
    quick.release.set()
    manager.channels = {"hung": hung, "quick": quick}
    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(m.record["message"]), level="WARNING")
    try:
        async with asyncio.timeout(3):
            await manager.stop_all()
    finally:
        logger.remove(sink_id)

    assert quick.stopped
    assert not hung.stopped
    assert any("hung" in m and "did not stop" in m for m in records)
