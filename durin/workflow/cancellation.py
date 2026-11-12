"""Process-global cancellation registry for workflow runs.

A background workflow run executes in a worker thread (``run_workflow`` ->
``run_on_workflow_thread`` -> ``WorkflowEngine.run``), so the agent that launched it
cannot reach into that thread to stop it. Instead the engine polls a cooperative
cancel flag: a run is cancelled by registering its ``run_id`` here, and the
engine checks it at the top of its node walk (either mode) plus mid-node
wherever a mid-node interrupt is possible.

Two modes:

- **graceful** (the default): the run stops at its next node boundary. A script
  node's subprocess is still killed mid-run (a subprocess has no partial value
  worth waiting for), but an in-flight work-node agent turn finishes first.
- **hard**: additionally interrupts an in-flight work-node turn — the node
  runner polls :func:`is_hard_cancelled` and aborts the turn.

``request_cancel`` upgrades graceful → hard and never downgrades, so a repeat
stop naturally escalates and a second graceful stop cannot undo a force-stop
already in flight. The registry is a module-level singleton (mirrors
``process_registry``) so the ``tasks`` tool — a different object than the engine
running the workflow — can signal a run by id. ``run_workflow`` registers a run
id before the walk and ``clear``s it when the run ends, so the map does not grow
without bound.

The registry lives in memory and goes away with the process. A stop that must
outlive it (so a run someone stopped is never offered for resume after its
process died) is also kept beside the run's record: see
``durin.workflow.run_log.request_stop``.

A process that is going down says so with :func:`begin_shutdown` before it
cancels the turns in flight. A turn cancelled by that shutdown is not anyone
stopping the run it waits on: the run is left to die with the process, to be
found dead, and resumable, by the gateway's crash sweep, the ``tasks`` tool or a
resume of it. :func:`shutting_down` is how the code
that cancels a run on a cancelled turn tells the two apart.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_cancelled: dict[str, str] = {}  # run_id -> "graceful" | "hard"

# Set once this process has begun shutting down (a signal, a restart, a quit).
# Never cleared: a process that began shutting down does not stop doing so.
_shutting_down = False


def begin_shutdown() -> None:
    """Mark this process as shutting down. Call it before the turns in flight
    are cancelled, so a cancelled turn is not taken for a stop of its run."""
    global _shutting_down
    _shutting_down = True


def shutting_down() -> bool:
    """Whether this process has begun shutting down."""
    return _shutting_down


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
