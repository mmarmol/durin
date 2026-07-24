"""Tests for the two-layer channel filter facts: the universal core every
channel populates, and the open per-channel bag.

The Slack payloads here are shaped after a real app notification (a Zendesk
ticket alert): the top-level text is empty and everything that identifies the
item lives in the attachment.
"""

from __future__ import annotations

from durin.bus.events import InboundMessage
from durin.loops.channel_meta import CHANNEL_FILTER_KEYS, extract
from durin.loops.matcher import TriggerMatcher


def _slack(content: str = "", **event) -> InboundMessage:
    return InboundMessage(
        channel="slack",
        sender_id=event.pop("user", "U0ALMFF79A9"),
        chat_id="C0GUARDSUP",
        content=content,
        metadata={"slack": {"event": event, "thread_ts": None, "channel_type": "channel"}},
    )


def _match(filters: dict, facts) -> bool:
    return TriggerMatcher._structural_match(filters, facts)


# --- the universal core ---


def test_slack_app_post_is_classified_as_a_bot_with_its_room_and_name():
    facts = extract(
        _slack(
            content="[shared] *Ticket #23106* | Status: new",
            subtype="bot_message",
            bot_id="B0AM3RCHBKK",
            app_id="A0221L31T4P",
            bot_profile={"name": "Zendesk"},
        )
    )

    assert facts.sender_kind == "bot"
    assert facts.sender_name == "Zendesk"
    assert facts.chat == "C0GUARDSUP"
    assert facts.extra == {"app_id": "A0221L31T4P", "bot_id": "B0AM3RCHBKK", "surface": "channel"}


def test_slack_person_post_is_classified_as_human():
    facts = extract(_slack(content="hola", user="U0HUMAN"))

    assert facts.sender_kind == "human"
    assert facts.sender_name is None
    assert facts.extra == {"surface": "channel"}


def test_a_channel_that_cannot_tell_sender_kind_says_nothing():
    """Absent is not "human": a filter for people must not match a message
    whose origin the channel never classified."""
    facts = extract(
        InboundMessage(
            channel="whatsapp", sender_id="+123", chat_id="+123", content="hi", metadata={}
        )
    )

    assert facts.sender_kind is None
    assert not _match({"sender_kind": "human"}, facts)


def test_every_supported_channel_reports_where_the_message_arrived():
    for channel in CHANNEL_FILTER_KEYS:
        msg = InboundMessage(
            channel=channel, sender_id="s", chat_id="room-9", content="x", metadata={}
        )
        assert extract(msg).chat == "room-9", channel


# --- exact vs substring matching ---


def test_room_filter_is_exact_so_a_prefix_does_not_leak():
    """A substring test would fire a #support loop inside #support-escalations."""
    facts = extract(_slack(content="x"))
    facts_wide = extract(
        InboundMessage(
            channel="slack",
            sender_id="U1",
            chat_id="C0GUARDSUP-ESCALATIONS",
            content="x",
            metadata={"slack": {}},
        )
    )

    assert _match({"chat": "C0GUARDSUP"}, facts)
    assert not _match({"chat": "C0GUARDSUP"}, facts_wide)


def test_text_filter_stays_a_substring_match():
    facts = extract(_slack(content="[shared] *Ticket #23106* | Status: new"))

    assert _match({"text_contains": "ticket #23106"}, facts)


def test_undeclared_key_matches_nothing_rather_than_everything():
    facts = extract(_slack(content="x"))

    assert not _match({"nonsense": "whatever"}, facts)


def test_bag_key_is_filterable_and_all_filters_must_hold():
    facts = extract(
        _slack(content="x", subtype="bot_message", bot_id="B0AM3RCHBKK", app_id="A0221L31T4P")
    )

    assert _match({"sender_kind": "bot", "chat": "C0GUARDSUP", "app_id": "A0221L31T4P"}, facts)
    assert not _match({"sender_kind": "bot", "app_id": "A-OTHER-APP"}, facts)


def test_empty_filters_match_everything():
    assert _match({}, extract(_slack(content="x")))
