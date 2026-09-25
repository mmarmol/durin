"""Channel-side rendering contract for user-facing tool payloads.

Interactive tools (``ask_user_question``, ``request_secret``,
``exit_plan_mode``) register structured payloads in ``session.metadata``.
Rich channels render those payloads natively from ``tool_events`` (webui
panels, TUI bubbles). Channels that cannot render structured payloads get
a plain-text fallback message published by the agent loop at turn end
(see ``AgentLoop._maybe_publish_interaction_fallback``).

This module is the single source of truth for (a) which channels render
payloads themselves and (b) how each pending payload serializes to text.
"""

from __future__ import annotations

import hashlib
from contextlib import suppress
from typing import Any, Mapping

# Channels whose UI renders tool payloads (question panels, plan cards,
# secret prompts) directly from structured ``tool_events``. Everything
# else gets the serialized fallback message.
RICH_PAYLOAD_CHANNELS = {"websocket", "cli"}

PENDING_SECRET_KEY = "pending_secret_request"
PENDING_PLAN_KEY = "pending_plan_review"
PENDING_APPROVAL_KEY = "pending_approval"

# Digest per payload key of what the text fallback already published, so a
# payload that outlives its turn is not re-sent on every later turn.
_DELIVERED_KEY = "_delivered_interactions"

_PLAN_FALLBACK_MAX_CHARS = 4_000


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def channel_renders_tool_payloads(channel: str | None) -> bool:
    """True when *channel* renders structured tool payloads in its own UI."""
    return bool(channel) and channel in RICH_PAYLOAD_CHANNELS


async def push_session_state(bus: Any, channel: str | None, chat_id: str | None,
                             metadata: Mapping[str, Any] | None) -> None:
    """Push the session-state snapshot a rich channel draws from, mid-turn.

    Rich channels (webui, TUI) otherwise receive it only at turn end. A turn
    that waits on the person (a blocking question, an in-chat approval)
    pushes it when the wait starts, so the question or approval card shows
    while the turn waits and the webui channel learns something is pending
    (a chat no tab is watching then stops waiting after a grace window), and
    again when the wait ends, so it clears. Best effort: a failed push never
    breaks the turn."""
    if bus is None or not chat_id or not channel_renders_tool_payloads(channel):
        return
    from durin.bus.events import OutboundMessage
    from durin.session.goal_state import goal_state_ws_blob

    with suppress(Exception):
        await bus.publish_outbound(OutboundMessage(
            channel=channel, chat_id=chat_id, content="",
            metadata={"_goal_state_sync": True, "goal_state": goal_state_ws_blob(metadata)},
        ))


def _serialize_question(payload: Mapping[str, Any]) -> str | None:
    question = str(payload.get("question") or "").strip()
    if not question:
        return None
    lines = [f"❓ {question}"]
    options = payload.get("options") or []
    for i, opt in enumerate(options, start=1):
        lines.append(f"{i}. {opt}")
    return "\n".join(lines)


def _serialize_secret_request(payload: Mapping[str, Any]) -> str | None:
    name = str(payload.get("name") or "").strip()
    service = str(payload.get("service") or "").strip()
    if not name or not service:
        return None
    purpose = str(payload.get("purpose") or "").strip()
    update = bool(payload.get("update"))
    if update:
        lines = [f"🔑 I need to replace the value of secret '{name}' ({service})."]
    else:
        lines = [f"🔑 I need the secret '{name}' for {service}."]
    if purpose:
        lines.append(f"Reason: {purpose}")
    if update:
        lines.append(
            "Please run this command and paste the new value at the hidden "
            "prompt (service, scope and description stay unchanged):"
        )
        lines.append(f"    durin secret set {name}")
    else:
        lines.append(
            "Please run this command and paste the secret at the hidden prompt "
            "(it goes straight to durin's secret store — never into the chat):"
        )
        lines.append(f"    durin secret set {name} --service {service} --scope exec")
    return "\n".join(lines)


def _serialize_plan_review(payload: Mapping[str, Any]) -> str | None:
    plan = str(payload.get("plan") or "").strip()
    path = str(payload.get("path") or "").strip()
    if not plan:
        return None
    verification = str(payload.get("verification") or "").strip()
    if verification:
        from durin.agent.tools.plan_mode import compose_plan_document

        plan = compose_plan_document(plan, verification).strip()
    if len(plan) > _PLAN_FALLBACK_MAX_CHARS:
        plan = plan[:_PLAN_FALLBACK_MAX_CHARS].rstrip() + "\n…(truncated)"
    tail = f"\n\nFull plan: {path}" if path else ""
    return (
        f"📋 Plan ready for review:\n\n{plan}{tail}\n\n"
        "Reply /build to approve and start execution, or send feedback to refine it."
    )


def _serialize_approval(payload: Mapping[str, Any]) -> str | None:
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        return None
    lines = [f"🔐 Approval needed: {summary}"]
    detail = payload.get("detail") or {}
    for key in ("command", "cwd", "rule", "verdict", "source", "server", "packages",
                "env", "headers", "security", "runtime"):
        value = detail.get(key) if isinstance(detail, Mapping) else None
        if value:
            lines.append(f"{key}: {value}")
    diff = detail.get("diff") if isinstance(detail, Mapping) else None
    if diff:
        lines.append(str(diff)[:1500])
    lines.append("Reply *yes* to approve or *no* to reject.")
    return "\n".join(lines)


_SERIALIZERS = (
    ("pending_question", _serialize_question),
    (PENDING_SECRET_KEY, _serialize_secret_request),
    (PENDING_PLAN_KEY, _serialize_plan_review),
    (PENDING_APPROVAL_KEY, _serialize_approval),
)


def pending_interaction_items(
    metadata: Mapping[str, Any] | None,
) -> list[tuple[str, str]]:
    """``(metadata_key, fallback_text)`` for every pending interaction."""
    if not metadata:
        return []
    out: list[tuple[str, str]] = []
    for key, fn in _SERIALIZERS:
        payload = metadata.get(key)
        if isinstance(payload, Mapping):
            text = fn(payload)
            if text:
                out.append((key, text))
    return out


def serialize_pending_interactions(metadata: Mapping[str, Any] | None) -> list[str]:
    """Plain-text fallback messages for every pending interaction in *metadata*."""
    return [text for _, text in pending_interaction_items(metadata)]


def undelivered_interactions(
    metadata: Mapping[str, Any] | None,
) -> list[tuple[str, str]]:
    """Pending interactions whose current text has not been published yet.

    Keyed on a digest of the text rather than on the metadata key alone: a
    payload the model *revises* (a refined plan, a re-asked question) reads as
    new and is delivered again, while an unchanged payload that outlives its
    turn — the plan waits for ``/build`` — is delivered exactly once.
    """
    delivered = (metadata or {}).get(_DELIVERED_KEY) or {}
    if not isinstance(delivered, Mapping):
        delivered = {}
    return [
        (key, text)
        for key, text in pending_interaction_items(metadata)
        if delivered.get(key) != _digest(text)
    ]


def mark_interactions_delivered(
    metadata: dict[str, Any], items: list[tuple[str, str]]
) -> None:
    """Record *items* (as returned by :func:`undelivered_interactions`) as sent."""
    delivered = metadata.get(_DELIVERED_KEY)
    if not isinstance(delivered, dict):
        delivered = {}
    for key, text in items:
        delivered[key] = _digest(text)
    metadata[_DELIVERED_KEY] = delivered


def forget_delivery(metadata: dict[str, Any], key: str) -> None:
    """Drop the delivered mark for *key*.

    Called when a new payload replaces the one under *key*, so it is
    delivered even when its text matches one sent earlier (the same question
    asked again after the first was answered).
    """
    delivered = metadata.get(_DELIVERED_KEY)
    if isinstance(delivered, dict):
        delivered.pop(key, None)


_EVENT_SERIALIZERS = {
    "ask_user_question": _serialize_question,
    "request_secret": _serialize_secret_request,
    "exit_plan_mode": _serialize_plan_review,
}


def format_interactive_tool_event(event: Mapping[str, Any] | None) -> str | None:
    """Plain-text rendering of an interactive tool_event's arguments.

    For surfaces that consume tool_events but render text only (the plain
    interactive CLI): the question/secret/plan must reach the user even
    though the model no longer re-presents it in prose.
    """
    if not isinstance(event, Mapping):
        return None
    fn = _EVENT_SERIALIZERS.get(str(event.get("name") or ""))
    if fn is None:
        return None
    arguments = event.get("arguments")
    if not isinstance(arguments, Mapping):
        return None
    return fn(arguments)
