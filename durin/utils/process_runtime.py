"""Process-level runtime markers.

The HTTP app factory marks when the serving process came up; the health
endpoint and the runtime-status service derive uptime from it. Lives in
``utils`` so both the API layer and services can read it without importing
each other.
"""

from __future__ import annotations

import time

_started_at: float | None = None


def mark_started() -> None:
    """Record process start once; later calls are no-ops."""
    global _started_at
    if _started_at is None:
        _started_at = time.time()


def uptime_s() -> float | None:
    """Seconds since :func:`mark_started`, or ``None`` if never marked."""
    if _started_at is None:
        return None
    return round(time.time() - _started_at, 1)
