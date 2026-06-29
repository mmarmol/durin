import pytest
from durin.bus.queue import MessageBus
from durin.bus.events import InboundMessage


def _msg(**kw):
    return InboundMessage(channel="telegram", sender_id="s", chat_id="c", content="hi", **kw)


async def test_publish_without_authorizer_enqueues():
    bus = MessageBus()
    await bus.publish_inbound(_msg())
    assert bus.inbound.qsize() == 1


async def test_authorizer_false_drops():
    bus = MessageBus()
    bus.set_inbound_authorizer(lambda m: False)
    await bus.publish_inbound(_msg())
    assert bus.inbound.qsize() == 0


async def test_authorizer_true_enqueues_and_is_dm_roundtrips():
    bus = MessageBus()
    bus.set_inbound_authorizer(lambda m: True)
    await bus.publish_inbound(_msg(is_dm=True))
    got = await bus.consume_inbound()
    assert got.is_dm is True
