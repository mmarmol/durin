import pytest
from durin.bus.queue import MessageBus
from durin.bus.events import InboundMessage


def _msg(**kw):
    return InboundMessage(channel="email", sender_id="s", chat_id="c", content="hi", **kw)


async def test_no_interceptors_enqueues():
    bus = MessageBus()
    await bus.publish_inbound(_msg())
    assert bus.inbound.qsize() == 1


async def test_interceptor_true_consumes_and_does_not_enqueue():
    bus = MessageBus()
    calls = []
    bus.add_inbound_interceptor(lambda m: calls.append(m) or True)
    await bus.publish_inbound(_msg())
    assert bus.inbound.qsize() == 0
    assert len(calls) == 1


async def test_interceptor_false_falls_through_to_enqueue():
    bus = MessageBus()
    bus.add_inbound_interceptor(lambda m: False)
    await bus.publish_inbound(_msg())
    assert bus.inbound.qsize() == 1


async def test_async_interceptor_is_awaited():
    bus = MessageBus()

    async def consume(_msg):
        return True

    bus.add_inbound_interceptor(consume)
    await bus.publish_inbound(_msg())
    assert bus.inbound.qsize() == 0


async def test_first_truthy_interceptor_wins_later_ones_skipped():
    bus = MessageBus()
    second_called = []
    bus.add_inbound_interceptor(lambda m: True)
    bus.add_inbound_interceptor(lambda m: second_called.append(m) or True)
    await bus.publish_inbound(_msg())
    assert second_called == []
    assert bus.inbound.qsize() == 0


async def test_interceptors_run_after_authorizer_denies():
    bus = MessageBus()
    intercepted = []
    bus.set_inbound_authorizer(lambda m: False)
    bus.add_inbound_interceptor(lambda m: intercepted.append(m) or True)
    await bus.publish_inbound(_msg())
    assert intercepted == []  # authorizer denied first; interceptor never ran
    assert bus.inbound.qsize() == 0


async def test_interceptors_run_after_authorizer_allows():
    bus = MessageBus()
    bus.set_inbound_authorizer(lambda m: True)
    bus.add_inbound_interceptor(lambda m: True)
    await bus.publish_inbound(_msg())
    assert bus.inbound.qsize() == 0


async def test_multiple_interceptors_all_false_enqueues():
    bus = MessageBus()
    bus.add_inbound_interceptor(lambda m: False)
    bus.add_inbound_interceptor(lambda m: False)
    await bus.publish_inbound(_msg())
    assert bus.inbound.qsize() == 1
