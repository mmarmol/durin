"""Builders for the live ``workflow_progress`` node frames.

The engine emits these as it walks (node started, node finished, parallel
branches) and the run_workflow tool emits a terminal one. All of them describe
the same thing — the state of every node the run has touched — so they are built
here once. Adding a field to a frame means adding it in this module only.
"""

from __future__ import annotations

from typing import Any

from durin.workflow.spec import node_label


def _label(workflow: Any, node_id: str) -> str:
    node = getattr(workflow, "nodes", {}).get(node_id)
    return node_label(node) if node is not None else node_id


def _finished_status(status: str) -> str:
    return "failed" if status in ("node_failed", "persist_failed") else "done"


def finished_frames(workflow: Any, runs: list[Any]) -> list[dict]:
    """One frame per accumulated ``NodeRun``, in visit order."""
    return [
        {
            "id": r.node_id,
            "label": _label(workflow, r.node_id),
            "status": _finished_status(r.status),
            "route_label": getattr(r, "route_label", None),
            "iteration": r.iteration,
            "budget": getattr(r, "budget", None),
        }
        for r in runs
    ]


def running_frame(node: Any, *, iteration: int, budget: int | None,
                  started_at: float | None = None) -> dict:
    """The frame for the node the engine is about to execute.

    ``started_at`` is wall-clock epoch seconds; surfaces derive the elapsed
    clock from it rather than counting frames, so the clock stays right across
    a reconnect that misses frames.
    """
    return {
        "id": node.id,
        "label": node_label(node),
        "status": "running",
        "route_label": None,
        "iteration": iteration,
        "budget": budget,
        "started_at": started_at,
    }
