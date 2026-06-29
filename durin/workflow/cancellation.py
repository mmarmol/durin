"""Process-global cancellation registry for workflow runs.

A background workflow run executes in a worker thread (``run_workflow`` ->
``asyncio.to_thread`` -> ``WorkflowEngine.run``), so the agent that launched it
cannot reach into that thread to stop it. Instead the engine polls a cooperative
cancel flag between nodes: a run is cancelled by setting its ``run_id`` here, and
the engine checks it at the top of its node walk. Cancellation is therefore
best-effort and takes effect *between* nodes — a node already executing finishes
first (the same best-effort contract as cancelling a sub-agent).

The registry is a module-level singleton (mirrors ``process_registry``) so the
``tasks`` tool — a different object than the engine running the workflow — can
signal a run by id. ``run_workflow`` registers a run id before the walk and
``clear``s it when the run ends, so the set does not grow without bound.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_cancelled: set[str] = set()


def request_cancel(run_id: str) -> None:
    """Mark ``run_id`` for cancellation. The engine stops before its next node."""
    with _lock:
        _cancelled.add(run_id)


def is_cancelled(run_id: str) -> bool:
    """Whether ``run_id`` has been asked to cancel."""
    with _lock:
        return run_id in _cancelled


def clear(run_id: str) -> None:
    """Forget ``run_id`` (run finished). Safe to call when not present."""
    with _lock:
        _cancelled.discard(run_id)
