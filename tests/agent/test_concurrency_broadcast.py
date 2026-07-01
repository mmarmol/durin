import asyncio

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.channels.websocket import publish_concurrency_snapshot


def test_publish_puts_global_frame_on_bus():
    bus = MessageBus()
    publish_concurrency_snapshot(bus, {"lanes": {}, "queued": 0, "work": []})
    msg = bus.outbound.get_nowait()
    assert msg.chat_id == "*"
    assert msg.metadata.get("_concurrency_snapshot") is True
    assert msg.metadata["snapshot"] == {"lanes": {}, "queued": 0, "work": []}


class _Stub:
    """Minimal host for the loop's coalescing methods (only touches
    ``self.bus``, ``self._concurrency_flush_scheduled`` and
    ``self.build_concurrency_snapshot``)."""
    mark_concurrency_dirty = AgentLoop.mark_concurrency_dirty
    _flush_concurrency_snapshot = AgentLoop._flush_concurrency_snapshot

    def __init__(self, bus):
        self.bus = bus
        self._concurrency_flush_scheduled = False
        self.builds = 0

    def build_concurrency_snapshot(self):
        self.builds += 1
        return {"lanes": {}, "queued": 0, "work": []}


@pytest.mark.asyncio
async def test_mark_dirty_coalesces_to_one_flush():
    bus = MessageBus()
    stub = _Stub(bus)
    stub.mark_concurrency_dirty()
    stub.mark_concurrency_dirty()
    stub.mark_concurrency_dirty()
    await asyncio.sleep(0)  # let call_soon run
    assert stub.builds == 1                 # coalesced
    assert bus.outbound.qsize() == 1        # one frame published


def test_mark_dirty_noop_without_running_loop():
    bus = MessageBus()
    stub = _Stub(bus)
    stub.mark_concurrency_dirty()  # no running loop -> silently skipped
    assert bus.outbound.qsize() == 0
    assert stub._concurrency_flush_scheduled is False
