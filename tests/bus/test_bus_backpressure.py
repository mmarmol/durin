import asyncio

from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus


def _msg(i):
    return InboundMessage(channel="cli", sender_id="u", chat_id="c", content=str(i))


def test_default_is_bounded():
    bus = MessageBus()
    assert bus.inbound.maxsize > 0
    assert bus.outbound.maxsize > 0


def test_zero_is_unbounded():
    bus = MessageBus(maxsize=0)
    assert bus.inbound.maxsize == 0
    assert bus.outbound.maxsize == 0


def test_put_blocks_when_full_then_unblocks():
    async def run():
        bus = MessageBus(maxsize=2)
        await bus.publish_inbound(_msg(1))
        await bus.publish_inbound(_msg(2))
        blocked = asyncio.create_task(bus.publish_inbound(_msg(3)))
        await asyncio.sleep(0.05)
        assert not blocked.done()
        got = await bus.consume_inbound()
        assert got.content == "1"
        await asyncio.wait_for(blocked, timeout=1)
        assert bus.inbound_size == 2

    asyncio.run(run())


def test_default_bus_maxsize_guards_bad_value(monkeypatch):
    from durin.bus.queue import _default_bus_maxsize

    monkeypatch.setenv("DURIN_BUS_MAXSIZE", "not-a-number")
    assert _default_bus_maxsize() == 10000

    monkeypatch.setenv("DURIN_BUS_MAXSIZE", "5")
    assert _default_bus_maxsize() == 5

    monkeypatch.delenv("DURIN_BUS_MAXSIZE", raising=False)
    assert _default_bus_maxsize() == 10000
