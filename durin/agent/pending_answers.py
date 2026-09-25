"""In-turn pending-answer registry for the blocking ``ask_user_question``.

The ask_user tool awaits a Future that is resolved by the agent loop when
the user replies, allowing the same turn to continue with the answer as the
tool result. A waiter that cannot be answered falls back to yield semantics
(``FALLBACK``) on the answer timeout, a media reply, or when the webui chat
it waits in has had no viewer for a grace window: the question stays in
session metadata and the user's next message answers it in a new turn.

A blocked turn does not survive a restart. At shutdown ``AgentLoop.stop``
cancels the waiters; the gateway journals the message each turn in flight
was answering and replays it on the next start, so the question is asked
again.
"""

from __future__ import annotations

import asyncio


class _Fallback:
    """Sentinel delivered when the waiter must fall back to yield semantics."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<pending_answers.FALLBACK>"


FALLBACK = _Fallback()

_WAITERS: dict[str, asyncio.Future] = {}

# session_key -> (kind, ref) for the live waiter registered on it. "question"
# waiters take the user's text verbatim; "approval" waiters (ref = the
# approval record id) accept only a yes/no verdict, parsed by the loop.
_KINDS: dict[str, tuple[str, str | None]] = {}

# True while AgentLoop.run()'s inbound consumer is active — the only thing
# that can ever resolve a waiter. Without it (single-message mode, tests),
# blocking would stall for the full timeout with nobody listening.
_CONSUMER_ACTIVE = False

# Sessions that never receive interactive replies: blocking there would
# always end in a useless timeout.
NON_INTERACTIVE_SESSION_PREFIXES = ("cron:", "system:")

# False while the only surface in this process cannot send a reply until the
# turn ends: the legacy prompt_toolkit REPL reads its next line only after the
# turn finishes, so a wait there could only end in the full timeout.
_MID_TURN_REPLIES = True


def set_consumer_active(active: bool) -> None:
    global _CONSUMER_ACTIVE
    _CONSUMER_ACTIVE = active


def set_mid_turn_replies(enabled: bool) -> None:
    """Declare whether this process's surface can send a reply mid-turn."""
    global _MID_TURN_REPLIES
    _MID_TURN_REPLIES = enabled


def consumer_active() -> bool:
    """True while an inbound consumer is alive and a user's answer can reach
    it before the turn ends."""
    return _CONSUMER_ACTIVE and _MID_TURN_REPLIES


def can_block(session_key: str | None) -> bool:
    """True when an in-turn wait on *session_key* could ever be answered.

    Delegates the context classification to ``durin.agent.approval`` so a
    context that cannot authorize a privileged action cannot answer a
    question either — one owner for "is a person actually there".
    """
    from durin.agent.approval import human_reachable

    return human_reachable(session_key)


def create(session_key: str, *, kind: str = "question", ref: str | None = None) -> asyncio.Future:
    """Register a fresh waiter for *session_key*, replacing any stale one.

    ``kind`` tells the loop how to read the reply: a ``question`` takes the
    user's text verbatim, an ``approval`` (``ref`` = the record id) takes
    only a yes/no verdict parsed by the server.

    Must be called from a coroutine: the future binds to the RUNNING loop
    (``get_event_loop`` could return a stale policy loop under test
    harnesses, making ``await`` hang forever).
    """
    stale = _WAITERS.pop(session_key, None)
    if stale is not None and not stale.done():
        stale.cancel()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _WAITERS[session_key] = fut
    _KINDS[session_key] = (kind, ref)
    return fut


def waiting_kind(session_key: str) -> str | None:
    """``question`` / ``approval`` for a live waiter, else None."""
    return _KINDS[session_key][0] if is_waiting(session_key) and session_key in _KINDS else None


def waiting_ref(session_key: str) -> str | None:
    """The approval record id a live approval waiter is for, else None."""
    return _KINDS[session_key][1] if is_waiting(session_key) and session_key in _KINDS else None


def is_waiting(session_key: str) -> bool:
    """True when a live (unresolved) waiter exists for *session_key*."""
    fut = _WAITERS.get(session_key)
    return fut is not None and not fut.done()


def _pop_live(session_key: str) -> asyncio.Future | None:
    fut = _WAITERS.get(session_key)
    if fut is None:
        return None
    del _WAITERS[session_key]
    _KINDS.pop(session_key, None)
    if fut.done():
        return None
    return fut


def resolve(session_key: str, text: str) -> bool:
    """Deliver *text* to the waiter. True when a live waiter consumed it."""
    fut = _pop_live(session_key)
    if fut is None:
        return False
    fut.set_result(text)
    return True


def fallback(session_key: str) -> bool:
    """Tell the waiter to fall back to yield semantics (e.g. media reply)."""
    fut = _pop_live(session_key)
    if fut is None:
        return False
    fut.set_result(FALLBACK)
    return True


def discard(session_key: str, fut: asyncio.Future) -> None:
    """Remove *fut* from the registry if it is still the registered waiter."""
    if _WAITERS.get(session_key) is fut:
        del _WAITERS[session_key]
        _KINDS.pop(session_key, None)


def reset() -> None:
    """Cancel all waiters and clear the consumer flag (shutdown and tests)."""
    global _CONSUMER_ACTIVE, _MID_TURN_REPLIES
    for fut in _WAITERS.values():
        if not fut.done():
            fut.cancel()
    _WAITERS.clear()
    _KINDS.clear()
    _CONSUMER_ACTIVE = False
    _MID_TURN_REPLIES = True
