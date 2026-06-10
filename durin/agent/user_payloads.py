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

from typing import Any, Mapping

# Channels whose UI renders tool payloads (question panels, plan cards,
# secret prompts) directly from structured ``tool_events``. Everything
# else gets the serialized fallback message.
RICH_PAYLOAD_CHANNELS = {"websocket", "cli"}

PENDING_SECRET_KEY = "pending_secret_request"
PENDING_PLAN_KEY = "pending_plan_review"

_PLAN_FALLBACK_MAX_CHARS = 4_000


def channel_renders_tool_payloads(channel: str | None) -> bool:
    """True when *channel* renders structured tool payloads in its own UI."""
    return bool(channel) and channel in RICH_PAYLOAD_CHANNELS


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
    lines = [f"🔑 I need the secret '{name}' for {service}."]
    if purpose:
        lines.append(f"Reason: {purpose}")
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
    if len(plan) > _PLAN_FALLBACK_MAX_CHARS:
        plan = plan[:_PLAN_FALLBACK_MAX_CHARS].rstrip() + "\n…(truncated)"
    tail = f"\n\nFull plan: {path}" if path else ""
    return (
        f"📋 Plan ready for review:\n\n{plan}{tail}\n\n"
        "Reply /build to approve and start execution, or send feedback to refine it."
    )


_SERIALIZERS = (
    ("pending_question", _serialize_question),
    (PENDING_SECRET_KEY, _serialize_secret_request),
    (PENDING_PLAN_KEY, _serialize_plan_review),
)


def serialize_pending_interactions(metadata: Mapping[str, Any] | None) -> list[str]:
    """Plain-text fallback messages for every pending interaction in *metadata*."""
    if not metadata:
        return []
    out: list[str] = []
    for key, fn in _SERIALIZERS:
        payload = metadata.get(key)
        if isinstance(payload, Mapping):
            text = fn(payload)
            if text:
                out.append(text)
    return out
