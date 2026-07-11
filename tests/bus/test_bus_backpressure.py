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


def test_raising_interceptor_is_passthrough():
    """A raising interceptor is skipped; message is enqueued if no other
    interceptor consumes it."""
    async def run():
        bus = MessageBus()

        def raising_interceptor(msg):
            raise ValueError("test error")

        def normal_interceptor(msg):
            return False  # does not consume

        bus.add_inbound_interceptor(raising_interceptor)
        bus.add_inbound_interceptor(normal_interceptor)

        msg = _msg(1)
        await bus.publish_inbound(msg)

        # Message should be enqueued despite the raising interceptor
        assert bus.inbound_size == 1
        got = await bus.consume_inbound()
        assert got.content == "1"

    asyncio.run(run())


def test_raising_interceptor_does_not_block_consumer():
    """A raising interceptor is skipped, but a consuming interceptor after it
    still consumes the message. Order is preserved."""
    async def run():
        bus = MessageBus()
        interceptor_calls = []

        def raising_interceptor(msg):
            interceptor_calls.append("raising")
            raise ValueError("test error")

        def consuming_interceptor(msg):
            interceptor_calls.append("consuming")
            return True  # consume the message

        bus.add_inbound_interceptor(raising_interceptor)
        bus.add_inbound_interceptor(consuming_interceptor)

        msg = _msg(1)
        await bus.publish_inbound(msg)

        # Both interceptors should have been called
        assert interceptor_calls == ["raising", "consuming"]
        # Message should NOT be enqueued (was consumed)
        assert bus.inbound_size == 0

    asyncio.run(run())


def test_raising_nameless_interceptor_is_passthrough():
    """A raising interceptor WITHOUT a __name__ (functools.partial, callable
    object) must not crash the error handler itself — message still enqueued."""
    import functools

    async def run():
        bus = MessageBus()

        def raising(msg, flavor):
            raise ValueError(f"test error {flavor}")

        bus.add_inbound_interceptor(functools.partial(raising, flavor="partial"))

        msg = _msg(1)
        await bus.publish_inbound(msg)

        assert bus.inbound_size == 1
        got = await bus.consume_inbound()
        assert got.content == "1"

    asyncio.run(run())
