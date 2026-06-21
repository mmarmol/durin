"""Per-session cross-process turn lease.

Holds a turn-scoped cross-process lock for the whole RESTORE..SAVE span so
the gateway and the TUI's own AgentLoop cannot run concurrent turns on one
session_key. Uses a separate ``.turn.lock`` file (distinct from the
``.lock`` used by save()) so the two locks are independent and the inner
save() call is never blocked by its own outer turn lease.

Acquired AFTER the in-process asyncio.Lock (fast path); the flock
auto-releases if the process dies.

Scope note: this lease serializes interactive TURNS per session across
processes. Direct out-of-turn savers do NOT yet acquire it — they take only
the save() ``.lock``, not this ``.turn.lock``. Known lease-bypassing paths:
  - HTTP rename in service/sessions.py
  - cron process_direct calls
  - webui background title-generation save
    (loop.py _schedule_background -> utils/webui_titles.py)
These are a known Phase-A limitation; closing them requires
background/direct-saver serialization work not yet scheduled.

See docs/architecture/concurrency.md for the full Phase-A rationale and
residual ledger.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from durin.utils.file_lock import cross_process_lock

# Generous acquire timeout: tool-heavy turns routinely take several minutes.
# Callers (the agent loop) catch TimeoutError and surface a clear message
# rather than dropping the turn silently.
_DEFAULT_TURN_LEASE_TIMEOUT = 600.0


def _turn_lock_path(session_path: Path) -> Path:
    """Return the turn-lease lock file path for a session.

    Uses a ``.turn.lock`` suffix so it is independent of the ``.lock`` file
    used by SessionManager.save(), which would cause a deadlock if the turn
    lease and save() tried to re-enter the same flock from the same fd.
    """
    return session_path.with_suffix(".turn.lock")


@asynccontextmanager
async def session_turn_lease(
    session_path: Path, *, timeout: float = _DEFAULT_TURN_LEASE_TIMEOUT
) -> AsyncIterator[None]:
    """Async context manager that holds the cross-process lock for one turn.

    Acquires the blocking flock via asyncio.to_thread so the event loop is
    not blocked during contention. Releases on exit (or if the process dies,
    the OS releases the flock automatically).

    ``timeout`` is how long a competing surface waits for the current turn to
    finish. When the timeout expires a ``TimeoutError`` is raised; callers
    should catch it and publish a clear user-facing message rather than
    letting the turn drop silently.
    """
    turn_path = _turn_lock_path(session_path)
    cm = cross_process_lock(turn_path, timeout=timeout)
    await asyncio.to_thread(cm.__enter__)
    try:
        yield
    finally:
        await asyncio.to_thread(cm.__exit__, None, None, None)
