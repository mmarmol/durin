"""Per-channel adapter: turn an inbound bus message into channel-agnostic
facts a loop can match/wake on, and turn a captured reply origin back into an
outbound message. Isolates the loop matcher/runtime from each channel's
metadata shape so only this module needs to track those contracts.

Facts come in two layers. The core fields are the ones every chat channel
really has — who sent it, what kind of sender, where it arrived, whether it
was private — named identically everywhere, so a loop written against them
carries across channels unchanged. Anything with no cross-channel meaning
goes in ``extra``: a flat, open bag each channel fills with its own
vocabulary. The matcher looks keys up there without interpreting them, so
adding a dimension touches only this module's branch for that channel.

Each channel also declares which keys it can populate (``CHANNEL_FILTER_KEYS``).
That declaration exists so the loop parser and the editor can warn about a
filter naming a key the target channel never emits — the failure it buys off
is a trigger that silently never fires because of a typo, or because a
Slack-shaped filter was pasted onto a Discord trigger. It is advisory only:
nothing consults it at match time, and a channel emitting an undeclared key
still matches normally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from durin.bus.events import InboundMessage, OutboundMessage


@dataclass(frozen=True)
class InboundFacts:
    sender: str  # channel-appropriate sender identity
    text: str  # message content
    title: str | None  # email subject; None on chat channels
    thread_key: str | None  # per-channel thread scoping key, or None
    reply: dict  # raw pieces build_reply needs later
    chat: str = ""  # identity of the room/chat the message arrived in
    chat_name: str | None = None  # readable room name, when the channel offers one
    sender_name: str | None = None  # readable sender name, when the channel offers one
    sender_kind: str | None = None  # "human", "bot", ...; None when the channel cannot tell
    is_dm: bool = False  # arrived in a private conversation
    extra: dict = field(default_factory=dict)  # channel-specific, open vocabulary


# Compared by exact equality (case-insensitive). Populated on every channel.
CORE_FILTER_KEYS = frozenset(
    {"sender", "sender_name", "sender_kind", "chat", "chat_name", "is_dm"}
)
# Compared as case-insensitive substrings. Unchanged from before this layering.
UNIVERSAL_CONTAINS_KEYS = frozenset({"from_contains", "sender_contains", "text_contains"})

# What each channel declares it can populate — the core keys, the substring
# keys that apply to it, and its own ``extra`` vocabulary. Advisory: see the
# module docstring.
CHANNEL_FILTER_KEYS: dict[str, frozenset[str]] = {
    "email": CORE_FILTER_KEYS | UNIVERSAL_CONTAINS_KEYS | frozenset({"subject_contains"}),
    "slack": CORE_FILTER_KEYS | UNIVERSAL_CONTAINS_KEYS | frozenset({"app_id", "bot_id", "surface"}),
    "telegram": CORE_FILTER_KEYS | UNIVERSAL_CONTAINS_KEYS | frozenset({"forum_topic"}),
    "discord": CORE_FILTER_KEYS | UNIVERSAL_CONTAINS_KEYS | frozenset({"guild", "thread_id"}),
    "whatsapp": CORE_FILTER_KEYS | UNIVERSAL_CONTAINS_KEYS | frozenset({"is_group"}),
}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _bag(*pairs: tuple[str, Any]) -> dict:
    """Flat extra-bag from (key, value) pairs, dropping empty values so an
    absent dimension is absent rather than an empty string that would match a
    filter nobody meant to write."""
    out = {}
    for key, value in pairs:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[key] = text
    return out


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
            chat=msg.chat_id,
            sender_name=meta.get("sender_name"),
            is_dm=msg.is_dm,
        )

    if channel == "slack":
        slack = _dict(meta.get("slack"))
        event = _dict(slack.get("event"))
        thread_ts = slack.get("thread_ts")
        thread_key = f"slack:{msg.chat_id}:{thread_ts}" if thread_ts else None
        # Slack marks app-authored posts either with the bot_message subtype
        # (no bot user) or with a bot_id alongside a real user id; both are
        # bots as far as a loop is concerned.
        is_bot = bool(event.get("bot_id")) or event.get("subtype") == "bot_message"
        return InboundFacts(
            sender=msg.sender_id,
            text=msg.content or "",
            title=None,
            thread_key=thread_key,
            reply={"thread_ts": thread_ts, "channel": msg.chat_id},
            chat=msg.chat_id,
            chat_name=slack.get("channel_name"),
            sender_name=_dict(event.get("bot_profile")).get("name"),
            sender_kind="bot" if is_bot else "human",
            is_dm=msg.is_dm,
            extra=_bag(
                ("app_id", event.get("app_id")),
                ("bot_id", event.get("bot_id")),
                ("surface", slack.get("channel_type")),
            ),
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
            chat=msg.chat_id,
            chat_name=meta.get("chat_title"),
            sender_name=meta.get("username"),
            sender_kind=_sender_kind(meta.get("is_bot")),
            is_dm=msg.is_dm,
            extra=_bag(("forum_topic", message_thread_id if meta.get("is_forum") else None)),
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
            chat=msg.chat_id,
            chat_name=meta.get("channel_name"),
            sender_name=meta.get("author_name"),
            sender_kind=_sender_kind(meta.get("author_is_bot")),
            is_dm=msg.is_dm,
            extra=_bag(("guild", meta.get("guild_id")), ("thread_id", meta.get("thread_id"))),
        )

    if channel == "whatsapp":
        message_id = meta.get("message_id")
        is_group = bool(meta.get("is_group"))
        thread_key = None if is_group else f"whatsapp:dm:{msg.chat_id}"
        return InboundFacts(
            sender=msg.sender_id,
            text=msg.content or "",
            title=None,
            thread_key=thread_key,
            reply={"message_id": message_id},
            chat=msg.chat_id,
            is_dm=msg.is_dm,
            extra=_bag(("is_group", "true" if is_group else "false")),
        )

    return None


def _sender_kind(is_bot: Any) -> str | None:
    """Map a channel's bot flag to a sender kind. None when the channel did
    not say — absent is not "human", so a filter for humans never matches a
    message whose origin the channel could not classify."""
    if is_bot is None:
        return None
    return "bot" if is_bot else "human"


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
