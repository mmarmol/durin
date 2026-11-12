"""Deliver a workflow run's result to the conversation that launched it.

A run launched by the agent from a conversation keeps that conversation's
address on its record (``launch``). Its result reaches that conversation as one
system message on the bus, keyed to the conversation's session: the agent loop
starts a turn there (or folds it into the running one) and tells the person.
This is how a background run's result arrives, and how a resumed run's result
arrives in the conversation that launched it when the resume was made
elsewhere — the dashboard, the API, or another conversation.

Delivery is best-effort: it never raises, so it never changes the outcome of
the run or of the resume that produced the result.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger

from durin.bus.events import InboundMessage

__all__ = ["deliver", "launch_session_key", "result_message"]

# The tag the agent loop recognises as a workflow result (it announces the turn
# it starts with a breadcrumb, and never takes it for the person's reply).
_EVENT = "workflow_background_result"
_SENDER = "workflow_background"
_CLOSING = ("Summarize the outcome for the user, and follow what the summary says about "
            "continuing the run.")


def launch_session_key(launch: Any) -> str | None:
    """The session key of the conversation a run was launched from, or None when
    the launch names no conversation (an automation, the service, an old record)."""
    if isinstance(launch, dict) and launch.get("kind") == "session" and launch.get("session_key"):
        return str(launch["session_key"])
    return None


def result_message(launch: Any, *, name: str, run_id: str, summary: str,
                   resumed_from: str | None = None) -> InboundMessage | None:
    """The message that hands ``summary`` to the conversation ``launch`` names,
    or None when it names none. ``resumed_from`` says where a resume made outside
    that conversation came from; without it the message reads as a background
    run finishing in the conversation that waits on it."""
    session_key = launch_session_key(launch)
    if session_key is None:
        return None
    channel = launch.get("channel") or "websocket"
    chat_id = launch.get("chat_id") or ""
    if resumed_from:
        # The head holds for every outcome (finished, paused again, aborted):
        # the summary below it says how the run ended.
        head = (f"[Workflow '{name}' run {run_id}, started from this conversation, was "
                f"resumed from {resumed_from}]")
    else:
        head = f"[Background workflow '{name}' finished]"
    return InboundMessage(
        channel="system",
        sender_id=_SENDER,
        chat_id=f"{channel}:{chat_id}",
        content=f"{head}\n\n{summary}\n\n{_CLOSING}",
        session_key_override=session_key,
        metadata={"injected_event": _EVENT, "workflow": name},
    )


async def deliver(bus: Any, launch: Any, *, name: str, run_id: str, summary: str,
                  resumed_from: str | None = None,
                  serves: Callable[[str], bool] | None = None) -> bool:
    """Publish the result message on ``bus``. False, with a warning in the log,
    when there is no bus, the launch names no conversation, the publish fails,
    or ``serves`` says this process does not serve the conversation's channel.

    ``serves`` is given when the conversation may belong to another process (a
    resume made outside the launch conversation): only a process that serves
    the channel may run a turn in it. It is None for the conversation the
    calling turn itself runs in."""
    msg = result_message(launch, name=name, run_id=run_id, summary=summary,
                         resumed_from=resumed_from)
    if msg is None:
        logger.warning("workflow {} run {}: its launch names no conversation; "
                       "the result is not delivered", name, run_id)
        return False
    channel = launch.get("channel") or "websocket"
    if serves is not None and not serves(channel):
        logger.warning("workflow {} run {}: the result is not delivered to {}: this process "
                       "does not serve channel {!r}; it stays in the run's record",
                       name, run_id, msg.session_key_override, channel)
        return False
    if bus is None:
        logger.warning("workflow {} run {}: no message bus on this surface; the result "
                       "is not delivered to {}", name, run_id, msg.session_key_override)
        return False
    try:
        await bus.publish_inbound(msg)
    except Exception:  # noqa: BLE001 - best-effort; the run's record already holds its outcome
        logger.opt(exception=True).warning(
            "workflow {} run {}: delivering the result to {} failed",
            name, run_id, msg.session_key_override)
        return False
    return True
