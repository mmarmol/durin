"""In-turn pending-answer registry for the blocking ``ask_user_question``.

The ask_user tool awaits a Future that is resolved by the agent loop when
the user replies, allowing the same turn to continue with the answer as the
tool result. A blocked turn cannot survive a restart — on timeout or
disconnect, the tool falls back to yielding, using session metadata to
maintain state.
"""

from __future__ import annotations

import asyncio


class _Fallback:
    """Sentinel delivered when the waiter must fall back to yield semantics."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<pending_answers.FALLBACK>"


FALLBACK = _Fallback()

_WAITERS: dict[str, asyncio.Future] = {}

# True while AgentLoop.run()'s inbound consumer is active — the only thing
# that can ever resolve a waiter. Without it (single-message mode, tests),
# blocking would stall for the full timeout with nobody listening.
_CONSUMER_ACTIVE = False

# Sessions that never receive interactive replies: blocking there would
# always end in a useless timeout.
NON_INTERACTIVE_SESSION_PREFIXES = ("cron:", "system:")


def set_consumer_active(active: bool) -> None:
    global _CONSUMER_ACTIVE
    _CONSUMER_ACTIVE = active


def can_block(session_key: str | None) -> bool:
    """True when an in-turn wait on *session_key* could ever be answered."""
    if not _CONSUMER_ACTIVE or not session_key:
        return False
    return not session_key.startswith(NON_INTERACTIVE_SESSION_PREFIXES)


def create(session_key: str) -> asyncio.Future:
    """Register a fresh waiter for *session_key*, replacing any stale one.

    Must be called from a coroutine: the future binds to the RUNNING loop
    (``get_event_loop`` could return a stale policy loop under test
    harnesses, making ``await`` hang forever).
    """
    stale = _WAITERS.pop(session_key, None)
    if stale is not None and not stale.done():
        stale.cancel()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _WAITERS[session_key] = fut
    return fut


def is_waiting(session_key: str) -> bool:
    """True when a live (unresolved) waiter exists for *session_key*."""
    fut = _WAITERS.get(session_key)
    return fut is not None and not fut.done()


def _pop_live(session_key: str) -> asyncio.Future | None:
    fut = _WAITERS.get(session_key)
    if fut is None:
        return None
    del _WAITERS[session_key]
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


def reset() -> None:
    """Clear all waiters and the consumer flag (tests)."""
    global _CONSUMER_ACTIVE
    for fut in _WAITERS.values():
        if not fut.done():
            fut.cancel()
    _WAITERS.clear()
    _CONSUMER_ACTIVE = False
