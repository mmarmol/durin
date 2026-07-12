"""Per-channel adapter: turn an inbound bus message into channel-agnostic
facts a loop can match/wake on, and turn a captured reply origin back into an
outbound message. Isolates the loop matcher/runtime from each channel's
metadata shape so only this module needs to track those contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from durin.bus.events import InboundMessage, OutboundMessage


@dataclass(frozen=True)
class InboundFacts:
    sender: str  # channel-appropriate sender identity
    text: str  # message content
    title: str | None  # email subject; None on chat channels
    thread_key: str | None  # per-channel thread scoping key, or None
    reply: dict  # raw pieces build_reply needs later


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def extract(msg: InboundMessage) -> InboundFacts | None:
    """Build InboundFacts from an inbound message, or None for an unsupported
    channel (e.g. cli, websocket, cron). Never raises on missing/malformed
    metadata keys."""
    meta = _dict(msg.metadata)
    channel = msg.channel

    if channel == "email":
        sender = meta.get("sender_email") or msg.sender_id
        thread = _dict(meta.get("email")).get("thread")
        return InboundFacts(
            sender=sender,
            text=msg.content or "",
            title=meta.get("subject"),
            thread_key=thread,
            reply={"thread": thread},
        )

    if channel == "slack":
        thread_ts = _dict(meta.get("slack")).get("thread_ts")
        thread_key = f"slack:{msg.chat_id}:{thread_ts}" if thread_ts else None
        return InboundFacts(
            sender=msg.sender_id,
            text=msg.content or "",
            title=None,
            thread_key=thread_key,
            reply={"thread_ts": thread_ts, "channel": msg.chat_id},
        )

    if channel == "telegram":
        message_thread_id = meta.get("message_thread_id")
        message_id = meta.get("message_id")
        if meta.get("is_forum") and message_thread_id is not None:
            thread_key = f"telegram:{msg.chat_id}:topic:{message_thread_id}"
        elif msg.is_dm:
            thread_key = f"telegram:dm:{msg.chat_id}"
        else:
            thread_key = None
        return InboundFacts(
            sender=msg.sender_id,
            text=msg.content or "",
            title=None,
            thread_key=thread_key,
            reply={"message_thread_id": message_thread_id, "message_id": message_id},
        )

    if channel == "discord":
        message_id = meta.get("message_id")
        if meta.get("thread_id"):
            thread_key = f"discord:thread:{msg.chat_id}"
        elif msg.is_dm:
            thread_key = f"discord:dm:{msg.chat_id}"
        else:
            thread_key = None
        return InboundFacts(
            sender=msg.sender_id,
            text=msg.content or "",
            title=None,
            thread_key=thread_key,
            reply={"chat_id": msg.chat_id, "message_id": message_id},
        )

    if channel == "whatsapp":
        message_id = meta.get("message_id")
        thread_key = None if meta.get("is_group") else f"whatsapp:dm:{msg.chat_id}"
        return InboundFacts(
            sender=msg.sender_id,
            text=msg.content or "",
            title=None,
            thread_key=thread_key,
            reply={"message_id": message_id},
        )

    return None


def build_reply(origin: dict, text: str) -> OutboundMessage:
    """Build the OutboundMessage for a captured reply origin, per the verified
    outbound contract of origin['channel']. Raises ValueError for a channel
    with no known reply contract; the caller guards this."""
    channel = origin["channel"]
    chat_id = origin["chat_id"]
    reply = _dict(origin.get("reply"))

    if channel == "email":
        # Pre-adapter run manifests carry no "reply" dict — their origin
        # recorded the thread digest at the top level only. Fall back to it
        # so a run parked before an upgrade still answers in-thread (a new
        # thread could never wake it). Only when "reply" is absent: a present
        # reply dict with thread=None means the origin genuinely had no
        # thread, and inventing one from other fields would be wrong.
        thread = reply.get("thread")
        if "reply" not in origin:
            thread = origin.get("thread")
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=text,
            metadata={"email": {"thread": thread}, "force_send": True},
        )

    if channel == "slack":
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=text,
            metadata={"slack": {"thread_ts": reply.get("thread_ts")}},
        )

    if channel == "telegram":
        metadata = {
            key: value
            for key, value in (
                ("message_thread_id", reply.get("message_thread_id")),
                ("message_id", reply.get("message_id")),
            )
            if value is not None
        }
        return OutboundMessage(channel=channel, chat_id=chat_id, content=text, metadata=metadata)

    if channel == "discord":
        return OutboundMessage(
            channel=channel,
            chat_id=reply.get("chat_id") or chat_id,
            content=text,
            reply_to=reply.get("message_id"),
        )

    if channel == "whatsapp":
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=text,
            reply_to=reply.get("message_id"),
        )

    raise ValueError(f"unsupported channel for reply: {channel!r}")
