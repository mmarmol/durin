"""Deterministic teardown for asyncio subprocess transports.

After a subprocess exits, asyncio leaves its transport for the garbage
collector. The transport's ``__del__`` then runs whenever GC fires —
frequently after the event loop that owned it has closed (pytest-asyncio
creates a fresh loop per test), which raises "Event loop is closed"
(surfaced as a ``PytestUnraisableExceptionWarning`` under tests, and a
noisy stray log in production).

Closing the transport while the loop is still alive makes teardown
deterministic and silences the warning. Call this after the process has
been awaited/reaped (``communicate``/``wait``).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any


async def aclose_subprocess(proc: Any | None) -> None:
    """Close *proc*'s transport inside the running loop (best-effort)."""
    if proc is None:
        return
    transport = getattr(proc, "_transport", None)
    # Guard against test doubles: a Mock's ``_transport`` is a truthy child
    # mock whose ``close()`` returns an unawaited coroutine. Only real
    # asyncio transports get closed.
    if not isinstance(transport, asyncio.BaseTransport):
        return
    with suppress(Exception):
        transport.close()
        # Let close()'s connection_lost callbacks run before the loop ends.
        await asyncio.sleep(0)
