"""Slack app/bot posts reaching loop triggers.

An app posting into a room (a ticket alert, a monitor, a CI digest) must reach
the trigger matcher even in a mention-only room, carry the attachment content
that identifies it, and never become a conversation durin answers.

The payload is shaped after a real Zendesk ticket notification: empty
top-level text, everything in the attachment's footer and fields.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from durin.bus.queue import MessageBus
from durin.channels.slack import SlackChannel, SlackConfig

ZENDESK_APP = "U0ALMFF79A9"
ROOM = "C0GUARDSUP"


def _event(**overrides) -> dict:
    event = {
        "type": "message",
        "subtype": "bot_message",
        "ts": "1784823066.607209",
        "user": ZENDESK_APP,
        "bot_id": "B0AM3RCHBKK",
        "app_id": "A0221L31T4P",
        "channel": ROOM,
        "channel_type": "channel",
        "text": "",
        "bot_profile": {"name": "Zendesk"},
        "attachments": [
            {
                "fallback": "This ticket needs attention",
                "text": "This ticket needs attention",
                "footer": "*Ticket #23106* | Status: new | Account: mxHero",
                "fields": [{"value": "*Requester*: Caller\n*Description*: Voicemail", "short": False}],
            }
        ],
    }
    event.update(overrides)
    return event


def _request(event: dict, envelope: str) -> SimpleNamespace:
    return SimpleNamespace(type="events_api", envelope_id=envelope, payload={"event": event})


def _channel(**config) -> SlackChannel:
    config.setdefault("enabled", True)
    config.setdefault("allow_from", [ZENDESK_APP])
    channel = SlackChannel(SlackConfig(**config), MessageBus())
    channel._bot_user_id = "UBOT"
    return channel


@pytest.mark.asyncio
async def test_app_post_reaches_the_bus_from_a_mention_only_room() -> None:
    """group_policy governs conversation, not events: a mention-only room must
    not hide app notifications from loop triggers."""
    channel = _channel(group_policy="mention")
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]

    await channel._on_socket_request(
        SimpleNamespace(send_socket_mode_response=AsyncMock()), _request(_event(), "env-bot-1")
    )

    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.await_args.kwargs
    assert kwargs["trigger_only"] is True
    assert kwargs["sender_id"] == ZENDESK_APP
    assert kwargs["chat_id"] == ROOM


@pytest.mark.asyncio
async def test_the_ticket_identity_survives_into_the_matched_text() -> None:
    """The whole point: correlate/text filters see the footer and fields even
    though Slack's top-level text is empty."""
    channel = _channel(group_policy="mention")
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]

    await channel._on_socket_request(
        SimpleNamespace(send_socket_mode_response=AsyncMock()), _request(_event(), "env-bot-2")
    )

    content = channel._handle_message.await_args.kwargs["content"]
    assert "Ticket #23106" in content
    assert "Voicemail" in content
    # "This ticket needs attention" appears in both text and fallback; the
    # extraction must not repeat it.
    assert content.count("This ticket needs attention") == 1


@pytest.mark.asyncio
async def test_an_unclaimed_app_post_never_reaches_the_agent() -> None:
    """No interceptor claimed it, so it is dropped rather than answered."""
    bus = MessageBus()
    channel = SlackChannel(SlackConfig(enabled=True, allow_from=[ZENDESK_APP]), bus)
    channel._bot_user_id = "UBOT"

    await channel._on_socket_request(
        SimpleNamespace(send_socket_mode_response=AsyncMock()), _request(_event(), "env-bot-3")
    )

    assert bus.inbound.empty()


@pytest.mark.asyncio
async def test_an_app_post_claimed_by_an_interceptor_is_seen_by_it() -> None:
    bus = MessageBus()
    seen: list[str] = []

    def interceptor(msg) -> bool:
        seen.append(msg.content)
        return True

    bus.add_inbound_interceptor(interceptor)
    channel = SlackChannel(SlackConfig(enabled=True, allow_from=[ZENDESK_APP]), bus)
    channel._bot_user_id = "UBOT"

    await channel._on_socket_request(
        SimpleNamespace(send_socket_mode_response=AsyncMock()), _request(_event(), "env-bot-4")
    )

    assert len(seen) == 1
    assert "Ticket #23106" in seen[0]


@pytest.mark.asyncio
async def test_durin_own_post_is_still_ignored() -> None:
    """Self-loop guard: durin's own message coming back must never re-enter."""
    channel = _channel(group_policy="open")
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]

    await channel._on_socket_request(
        SimpleNamespace(send_socket_mode_response=AsyncMock()),
        _request(_event(user="UBOT"), "env-bot-5"),
    )

    channel._handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_own_post_without_a_user_field_is_ignored_by_bot_id() -> None:
    """A self-post shaped as a webhook-style bot_message carries no user id,
    so only the bot_id check can catch it — and it must."""
    channel = _channel(group_policy="open")
    channel._bot_id = "B0DURINOWN"
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]

    event = _event(bot_id="B0DURINOWN")
    event.pop("user")
    await channel._on_socket_request(
        SimpleNamespace(send_socket_mode_response=AsyncMock()), _request(event, "env-bot-6")
    )

    channel._handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_webhook_style_app_post_is_identified_by_its_bot_id() -> None:
    """No user field at all: authorization keys on the bot_id instead."""
    channel = _channel(group_policy="mention", allow_from=["B0AM3RCHBKK"])
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]

    event = _event()
    event.pop("user")
    await channel._on_socket_request(
        SimpleNamespace(send_socket_mode_response=AsyncMock()), _request(event, "env-bot-7")
    )

    channel._handle_message.assert_awaited_once()
    kwargs = channel._handle_message.await_args.kwargs
    assert kwargs["sender_id"] == "B0AM3RCHBKK"
    assert kwargs["trigger_only"] is True


@pytest.mark.asyncio
async def test_a_person_still_needs_to_mention_durin_in_a_mention_only_room() -> None:
    """The unlock is scoped to apps; human routing is untouched."""
    channel = _channel(group_policy="mention", allow_from=["U0HUMAN"])
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]

    await channel._on_socket_request(
        SimpleNamespace(send_socket_mode_response=AsyncMock()),
        _request(
            {
                "type": "message",
                "user": "U0HUMAN",
                "channel": ROOM,
                "channel_type": "channel",
                "text": "just chatting",
                "ts": "1784823066.607300",
            },
            "env-human-1",
        ),
    )

    channel._handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_edits_and_joins_are_still_discarded() -> None:
    channel = _channel(group_policy="open")
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]

    for i, subtype in enumerate(("message_changed", "message_deleted", "channel_join")):
        await channel._on_socket_request(
            SimpleNamespace(send_socket_mode_response=AsyncMock()),
            _request(_event(subtype=subtype), f"env-sub-{i}"),
        )

    channel._handle_message.assert_not_awaited()
