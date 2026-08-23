"""Event types for the message bus."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Optional ``OutboundMessage.metadata`` key for structured, channel-agnostic UI
# payloads. Value is JSON-serializable with at least ``kind``; rich clients may
# render it and other channels may ignore unknown keys.
OUTBOUND_META_AGENT_UI = "_agent_ui"


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # telegram, discord, slack, whatsapp
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    session_key_override: str | None = None  # Optional override for thread-scoped sessions
    is_dm: bool = False  # True when the message arrived in a private/direct-message chat
    # True for messages published so loop triggers can see them, but which must
    # never become a conversation — an app posting alerts into a room. They run
    # the normal authorization gate and interceptors; if no interceptor claims
    # one, it is dropped instead of handed to the agent.
    trigger_only: bool = False

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel.

    ``metadata`` can carry routing (``message_id``, …), trace flags (``_progress``),
    and optional ``OUTBOUND_META_AGENT_UI`` blobs for rich clients; non-WebUI
    channels may ignore unknown keys.
    """

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    buttons: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class SendReceipt:
    """Identifies the thread a channel ``send`` landed in.

    ``thread_key`` follows the same per-channel vocabulary the inbound loop
    matcher keys claims on (e.g. ``slack:<chat_id>:<thread_ts>``, or the bare
    email thread digest with no channel prefix), so a claim registered from a
    send's receipt is woken by the reply that later lands on that thread.
    ``None`` when the channel could not determine a thread identity for this
    send (or does not implement receipts at all).
    """

    thread_key: str | None

