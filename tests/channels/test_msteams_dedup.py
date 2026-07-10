from durin.bus.queue import MessageBus
from durin.channels.msteams import MSTeamsChannel, MSTeamsConfig


def _make_channel(bus: MessageBus | None = None) -> MSTeamsChannel:
    config = MSTeamsConfig(
        enabled=True,
        app_id="app",
        app_password="secret",
        allow_from=["*"],
    )
    return MSTeamsChannel(config, bus or MessageBus())


def _activity(activity_id: str = "activity-1") -> dict:
    return {
        "type": "message",
        "id": activity_id,
        "text": "hello",
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "from": {"id": "user1", "name": "Alice"},
        "recipient": {"id": "bot1"},
        "conversation": {"id": "conv1", "conversationType": "personal"},
    }


async def test_handle_activity_dedups_retried_webhook_delivery() -> None:
    """Bot Framework retries webhook deliveries it considers unacknowledged;
    the same activity id delivered twice must publish only once."""
    bus = MessageBus()
    channel = _make_channel(bus)

    activity = _activity()
    await channel._handle_activity(activity)
    await channel._handle_activity(activity)

    msg = await bus.consume_inbound()
    assert msg.content == "hello"
    assert msg.chat_id == "conv1"
    assert bus.inbound_size == 0


async def test_handle_activity_does_not_dedup_distinct_activity_ids() -> None:
    """Distinct activity ids must not be treated as duplicates of each other."""
    bus = MessageBus()
    channel = _make_channel(bus)

    await channel._handle_activity(_activity("activity-1"))
    await channel._handle_activity(_activity("activity-2"))

    assert bus.inbound_size == 2
