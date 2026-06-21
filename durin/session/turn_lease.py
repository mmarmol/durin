"""Per-session cross-process turn lease.

Holds a turn-scoped cross-process lock for the whole RESTORE..SAVE span so
the gateway and the TUI's own AgentLoop cannot run concurrent turns on one
session_key. Uses a separate ``.turn.lock`` file (distinct from the
``.lock`` used by save()) so the two locks are independent and the inner
save() call is never blocked by its own outer turn lease.

Acquired AFTER the in-process asyncio.Lock (fast path); the flock
auto-releases if the process dies.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from durin.utils.file_lock import cross_process_lock


def _turn_lock_path(session_path: Path) -> Path:
    """Return the turn-lease lock file path for a session.

    Uses a ``.turn.lock`` suffix so it is independent of the ``.lock`` file
    used by SessionManager.save(), which would cause a deadlock if the turn
    lease and save() tried to re-enter the same flock from the same fd.
    """
    return session_path.with_suffix(".turn.lock")


@asynccontextmanager
async def session_turn_lease(
    session_path: Path, *, timeout: float = 30.0
) -> AsyncIterator[None]:
    """Async context manager that holds the cross-process lock for one turn.

    Acquires the blocking flock via asyncio.to_thread so the event loop is
    not blocked during contention. Releases on exit (or if the process dies,
    the OS releases the flock automatically).

    ``timeout`` is how long a competing surface waits for the current turn to
    finish. NOTE: a tool-heavy turn can take several minutes; callers that
    wire this into the agent loop should use a large timeout (or math.inf for
    unbounded wait) and handle TimeoutError explicitly rather than letting it
    propagate as an unhandled exception.
    """
    turn_path = _turn_lock_path(session_path)
    cm = cross_process_lock(turn_path, timeout=timeout)
    await asyncio.to_thread(cm.__enter__)
    try:
        yield
    finally:
        await asyncio.to_thread(cm.__exit__, None, None, None)
