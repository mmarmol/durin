"""Build the in-memory concurrency snapshot broadcast to the webui.

Pure function of the lane gates + running-work lists so it is trivially
testable and does zero I/O. The snapshot drives the sidebar saturation chip,
the Concurrency settings card's live readouts, and the global Work panel.
``limit == 0`` on a lane means unlimited (the UI renders it as ``∞``).
"""

from __future__ import annotations

from typing import Any

from durin.utils.resizable_semaphore import ResizableSemaphore


def build_snapshot(
    *,
    interactive: ResizableSemaphore,
    ceiling: ResizableSemaphore,
    subagent_running: int,
    subagent_limit: int,
    turn_sessions: list[str],
    running_subagents: list[tuple[str, str | None, str]],
) -> dict[str, Any]:
    """Assemble the snapshot dict. ``running_subagents`` is a list of
    ``(task_id, session_key, label)``; ``turn_sessions`` a list of session keys
    with a live interactive turn."""
    work: list[dict[str, Any]] = []
    for sk in turn_sessions:
        work.append({
            "kind": "turn", "id": f"turn:{sk}", "session_key": sk,
            "label": sk, "status": "running",
        })
    for task_id, sk, label in running_subagents:
        work.append({
            "kind": "subagent", "id": f"subagent:{task_id}", "session_key": sk,
            "label": label, "status": "running",
        })
    return {
        "lanes": {
            "interactive": {
                "active": interactive.active, "limit": interactive.limit,
                "waiting": interactive.waiting,
            },
            "ceiling": {
                "active": ceiling.active, "limit": ceiling.limit,
                "waiting": ceiling.waiting,
            },
            "subagents": {"active": subagent_running, "limit": subagent_limit},
        },
        "queued": interactive.waiting + ceiling.waiting,
        "work": work,
    }
