"""Tell the chat that asked for an approval how it was decided later.

A request decided outside the turn that filed it (from the Pending page, or a
click that lands after that turn stopped waiting) never reaches that turn: the
model was told the request is waiting and moved on. When the request came from
a chat session, a system note is posted into that session the same way a
background workflow's result is — a ``channel="system"`` inbound message keyed
to the session — so the agent learns the outcome and tells the person.

No note when:

* the verdict was handed to a turn still waiting on it (that turn reports it);
* nothing was decided (refused, or expired before anyone decided it);
* the request came from a context with no person (cron, a workflow…);
* the chat belongs to a channel this process does not serve, such as a TUI
  session (``cli:``) owned by another process — running a turn in it here
  would write to that session behind its owner's back;
* the session key names no single chat (``unified:`` folds every channel's
  conversation into one key, so there is no one chat to answer in).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from loguru import logger

from durin.agent.approval import Outcome, is_interactive
from durin.bus.events import InboundMessage

# The note's sender and the ``injected_event`` it carries, so the loop never
# takes it for the person's reply to a question the chat waits on.
NOTE_SENDER = "approval_decision"

# How much of an executor's result the note quotes: enough to say what
# happened, without flooding the turn that reads it.
_RESULT_MAX_CHARS = 1000


def chat_route(session_key: str | None) -> tuple[str, str] | None:
    """The ``(channel, chat_id)`` a session's replies are sent to, or None.

    A session key is ``<channel>:<chat_id>`` unless the channel scopes it to a
    thread, and then the thread part must not reach the channel as its chat
    id. Slack, email and Feishu append the thread after the chat id (the agent
    loop re-derives a Slack or email thread from the key when it answers);
    Discord names the thread's own channel after ``:thread:``, which is the
    chat to send to; a Telegram topic follows the chat id after ``:topic:``.
    Any other channel keeps the whole rest as the chat id, since its ids may
    contain colons (a Matrix room id does). ``unified:`` names no chat.
    """
    channel, sep, rest = (session_key or "").partition(":")
    if not sep or not channel or not rest or channel == "unified":
        return None
    if channel == "discord" and ":thread:" in rest:
        return channel, rest.rsplit(":thread:", 1)[1]
    if channel == "telegram" and ":topic:" in rest:
        return channel, rest.split(":topic:", 1)[0]
    if channel in ("slack", "email", "feishu"):
        return channel, rest.split(":", 1)[0]
    return channel, rest


def _result_text(result: Any) -> str:
    if not result:
        return "done"
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(result)
    if len(text) > _RESULT_MAX_CHARS:
        text = text[:_RESULT_MAX_CHARS].rstrip() + "…"
    return text


def _note_line(outcome: Outcome, record: dict) -> tuple[str, str] | None:
    """The note's verdict line and what the agent should do with it, or None
    when this outcome decided nothing."""
    summary = record.get("summary") or record.get("id") or "the request"
    if outcome.status == "rejected":
        return (f"Rejected: {summary}",
                "Tell the user it was declined. Do not retry it, and do not reach "
                "the same effect another way.")
    if outcome.status == "applied":
        result = _result_text(outcome.result if outcome.result is not None
                              else record.get("result"))
        return (f"Approved: {summary} — result: {result}",
                "Tell the user it was approved and what it did.")
    if outcome.status == "failed":
        error = (record.get("result") or {}).get("error") or outcome.message or "unknown error"
        return (f"Approved: {summary} — result: failed: {error}",
                "Tell the user it was approved but running it failed, and why.")
    if outcome.status == "stale" and record.get("status") == "stale":
        # Approved, then found its target changed since it was reviewed; an
        # expired record ("expired") was never decided at all.
        return (f"Approved: {summary} — result: not run: what it would change has "
                "changed since it was requested",
                "Tell the user it was approved but not run; ask again if it is still "
                "needed.")
    return None


def origin_note(outcome: Outcome, *, serves: Callable[[str], bool]) -> InboundMessage | None:
    """The system note for *outcome*, or None when none is owed (see the
    module docstring). *serves* says whether this process serves a channel."""
    record = outcome.record
    if record is None:
        return None
    session_key = record.get("requested_by_session")
    if not is_interactive(session_key):
        return None
    route = chat_route(session_key)
    if route is None or not serves(route[0]):
        return None
    line = _note_line(outcome, record)
    if line is None:
        return None
    verdict, instruction = line
    channel, chat_id = route
    return InboundMessage(
        channel="system",
        sender_id=NOTE_SENDER,
        chat_id=f"{channel}:{chat_id}",
        content=f"[Approval decided outside this chat]\n\n{verdict}\n\n{instruction}",
        session_key_override=session_key,
        metadata={"injected_event": NOTE_SENDER, "approval_id": record.get("id")},
    )


async def notify_origin(bus: Any, outcome: Outcome, *, serves: Callable[[str], bool]) -> bool:
    """Post *outcome*'s note on *bus*; True when one was posted. Best-effort:
    the decision is already recorded, so a failed post is logged, not raised."""
    note = origin_note(outcome, serves=serves)
    if note is None or bus is None:
        return False
    try:
        await bus.publish_inbound(note)
    except Exception:  # noqa: BLE001 — the record already holds the outcome
        logger.exception("could not post the decision note for approval {}",
                         note.metadata.get("approval_id"))
        return False
    return True
