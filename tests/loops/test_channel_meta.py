"""Tests for the per-channel inbound facts + reply adapter."""

from __future__ import annotations

import pytest

from durin.bus.events import InboundMessage, OutboundMessage
from durin.loops.channel_meta import build_reply, extract


def _msg(channel: str, **kw) -> InboundMessage:
    kw.setdefault("sender_id", "sender-1")
    kw.setdefault("chat_id", "chat-1")
    kw.setdefault("content", "hello")
    kw.setdefault("metadata", {})
    return InboundMessage(channel=channel, **kw)


# --- email ---


def test_email_extract():
    msg = _msg(
        "email",
        sender_id="alice@example.com",
        chat_id="alice@example.com",
        metadata={
            "sender_email": "alice@example.com",
            "subject": "Hi",
            "email": {"thread": "digest-1"},
        },
    )
    facts = extract(msg)
    assert facts.sender == "alice@example.com"
    assert facts.title == "Hi"
    assert facts.thread_key == "digest-1"
    assert facts.reply == {"thread": "digest-1"}


def test_email_extract_falls_back_to_sender_id_without_metadata():
    msg = _msg("email", sender_id="bob@example.com", metadata={})
    facts = extract(msg)
    assert facts.sender == "bob@example.com"
    assert facts.title is None
    assert facts.thread_key is None
    assert facts.reply == {"thread": None}


def test_email_build_reply():
    origin = {"channel": "email", "chat_id": "alice@example.com", "reply": {"thread": "digest-1"}}
    out = build_reply(origin, "reply text")
    assert isinstance(out, OutboundMessage)
    assert out.channel == "email"
    assert out.chat_id == "alice@example.com"
    assert out.content == "reply text"
    assert out.metadata == {"email": {"thread": "digest-1"}, "force_send": True}


def test_email_build_reply_pre_adapter_manifest_falls_back_to_origin_thread():
    # A run parked as waiting_info before the adapter existed has no "reply"
    # dict in its origin — only the top-level thread digest. The reply must
    # still land in-thread or the counterpart's answer can never wake it.
    origin = {"channel": "email", "chat_id": "alice@example.com", "thread": "digest-old"}
    out = build_reply(origin, "reply text")
    assert out.metadata == {"email": {"thread": "digest-old"}, "force_send": True}


def test_email_build_reply_present_reply_without_thread_stays_threadless():
    # reply present but thread=None = the origin genuinely had no thread;
    # the top-level field must NOT override it.
    origin = {
        "channel": "email",
        "chat_id": "alice@example.com",
        "thread": "custom:loop:42",
        "reply": {"thread": None},
    }
    out = build_reply(origin, "reply text")
    assert out.metadata == {"email": {"thread": None}, "force_send": True}


# --- slack ---


def test_slack_extract_with_thread():
    msg = _msg(
        "slack",
        chat_id="C123",
        metadata={"slack": {"thread_ts": "111.222", "channel_type": "channel"}},
    )
    facts = extract(msg)
    assert facts.thread_key == "slack:C123:111.222"
    assert facts.reply == {"thread_ts": "111.222", "channel": "C123"}


def test_slack_extract_without_thread():
    msg = _msg("slack", chat_id="C123", metadata={"slack": {"thread_ts": None}})
    facts = extract(msg)
    assert facts.thread_key is None
    assert facts.reply == {"thread_ts": None, "channel": "C123"}


def test_slack_extract_empty_metadata_no_exception():
    msg = _msg("slack", metadata={})
    facts = extract(msg)
    assert facts.thread_key is None
    assert facts.reply == {"thread_ts": None, "channel": "chat-1"}


def test_slack_build_reply():
    origin = {
        "channel": "slack",
        "chat_id": "C123",
        "reply": {"thread_ts": "111.222", "channel": "C123"},
    }
    out = build_reply(origin, "reply text")
    assert out.channel == "slack"
    assert out.chat_id == "C123"
    assert out.metadata == {"slack": {"thread_ts": "111.222"}}


# --- telegram ---


def test_telegram_extract_forum_topic():
    msg = _msg(
        "telegram",
        chat_id="500",
        metadata={"message_thread_id": 42, "is_forum": True, "message_id": 7},
    )
    facts = extract(msg)
    assert facts.thread_key == "telegram:500:topic:42"
    assert facts.reply == {"message_thread_id": 42, "message_id": 7}


def test_telegram_extract_dm_no_topic():
    msg = _msg(
        "telegram",
        chat_id="500",
        is_dm=True,
        metadata={"message_thread_id": None, "is_forum": False, "message_id": 9},
    )
    facts = extract(msg)
    assert facts.thread_key == "telegram:dm:500"
    assert facts.reply == {"message_thread_id": None, "message_id": 9}


def test_telegram_extract_group_no_topic():
    msg = _msg(
        "telegram",
        chat_id="500",
        is_dm=False,
        metadata={"message_thread_id": None, "is_forum": False, "message_id": 9},
    )
    facts = extract(msg)
    assert facts.thread_key is None


def test_telegram_build_reply_both_present():
    origin = {
        "channel": "telegram",
        "chat_id": "500",
        "reply": {"message_thread_id": 42, "message_id": 7},
    }
    out = build_reply(origin, "reply text")
    assert out.channel == "telegram"
    assert out.chat_id == "500"
    assert out.metadata == {"message_thread_id": 42, "message_id": 7}


def test_telegram_build_reply_drops_none_values():
    origin = {
        "channel": "telegram",
        "chat_id": "500",
        "reply": {"message_thread_id": None, "message_id": 9},
    }
    out = build_reply(origin, "reply text")
    assert out.metadata == {"message_id": 9}


# --- discord ---


def test_discord_extract_thread():
    msg = _msg(
        "discord", chat_id="chan-thread", metadata={"thread_id": "chan-thread", "message_id": "99"}
    )
    facts = extract(msg)
    assert facts.thread_key == "discord:thread:chan-thread"
    assert facts.reply == {"chat_id": "chan-thread", "message_id": "99"}


def test_discord_extract_dm():
    msg = _msg("discord", chat_id="user-1", is_dm=True, metadata={"message_id": "99"})
    facts = extract(msg)
    assert facts.thread_key == "discord:dm:user-1"


def test_discord_extract_group_no_thread():
    msg = _msg("discord", chat_id="chan-1", is_dm=False, metadata={"message_id": "99"})
    facts = extract(msg)
    assert facts.thread_key is None


def test_discord_build_reply():
    origin = {
        "channel": "discord",
        "chat_id": "chan-thread",
        "reply": {"chat_id": "chan-thread", "message_id": "99"},
    }
    out = build_reply(origin, "reply text")
    assert out.channel == "discord"
    assert out.chat_id == "chan-thread"
    assert out.reply_to == "99"


# --- whatsapp ---


def test_whatsapp_extract_dm():
    msg = _msg("whatsapp", chat_id="1555@lid", metadata={"message_id": "wamid1", "is_group": False})
    facts = extract(msg)
    assert facts.thread_key == "whatsapp:dm:1555@lid"
    assert facts.reply == {"message_id": "wamid1"}


def test_whatsapp_extract_group():
    msg = _msg("whatsapp", chat_id="grp@lid", metadata={"message_id": "wamid1", "is_group": True})
    facts = extract(msg)
    assert facts.thread_key is None


def test_whatsapp_build_reply():
    origin = {"channel": "whatsapp", "chat_id": "1555@lid", "reply": {"message_id": "wamid1"}}
    out = build_reply(origin, "reply text")
    assert out.channel == "whatsapp"
    assert out.chat_id == "1555@lid"
    assert out.reply_to == "wamid1"


# --- unsupported / malformed ---


@pytest.mark.parametrize("channel", ["cli", "websocket", "cron"])
def test_unsupported_channel_returns_none(channel):
    msg = _msg(channel)
    assert extract(msg) is None


def test_extract_never_raises_on_malformed_metadata():
    # None metadata must never raise; whatsapp treats missing is_group as a
    # DM (its documented default), every other channel yields no thread key.
    expected_thread_key = {
        "email": None,
        "slack": None,
        "telegram": None,
        "discord": None,
        "whatsapp": "whatsapp:dm:chat-1",
    }
    for channel, expected in expected_thread_key.items():
        msg = _msg(channel, metadata=None)
        facts = extract(msg)
        assert facts.thread_key == expected


def test_build_reply_unknown_channel_raises():
    with pytest.raises(ValueError):
        build_reply({"channel": "cli", "chat_id": "x", "reply": {}}, "text")
