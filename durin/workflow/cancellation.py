"""Process-global cancellation registry for workflow runs.

A background workflow run executes in a worker thread (``run_workflow`` ->
``asyncio.to_thread`` -> ``WorkflowEngine.run``), so the agent that launched it
cannot reach into that thread to stop it. Instead the engine polls a cooperative
cancel flag: a run is cancelled by registering its ``run_id`` here, and the
engine checks it at the top of its node walk (every mode) plus mid-node where a
mid-node interrupt is possible.

Two modes:

- **graceful** (default): the run stops at its next node boundary. A script
  node's subprocess is still killed mid-run (a subprocess has no partial value
  worth waiting for), but an in-flight work-node agent turn finishes first.
- **hard**: additionally interrupts an in-flight work-node turn — the node
  runner polls :func:`is_hard_cancelled` and aborts the turn.

``request_cancel`` upgrades graceful → hard and never downgrades, so a repeat
stop naturally escalates. The registry is a module-level singleton (mirrors
``process_registry``) so the ``tasks`` tool — a different object than the
engine running the workflow — can signal a run by id. ``run_workflow``
registers a run id before the walk and ``clear``s it when the run ends, so the
map does not grow without bound.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_cancelled: dict[str, str] = {}  # run_id -> "graceful" | "hard"


def request_cancel(run_id: str, *, hard: bool = False) -> None:
    """Mark ``run_id`` for cancellation. ``hard`` upgrades, never downgrades."""
    with _lock:
        if hard or run_id not in _cancelled:
            _cancelled[run_id] = "hard" if hard else "graceful"


def is_cancelled(run_id: str) -> bool:
    """Whether ``run_id`` has been asked to cancel (either mode)."""
    with _lock:
        return run_id in _cancelled


def is_hard_cancelled(run_id: str) -> bool:
    """Whether ``run_id`` has been asked to cancel HARD (interrupt mid-node)."""
    with _lock:
        return _cancelled.get(run_id) == "hard"


def clear(run_id: str) -> None:
    """Forget ``run_id`` (run finished). Safe to call when not present."""
    with _lock:
        _cancelled.pop(run_id, None)
