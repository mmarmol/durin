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
