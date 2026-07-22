"""Builders for the live ``workflow_progress`` node frames.

The engine emits these as it walks (node started, node finished, parallel
branches) and the run_workflow tool emits a terminal one. All of them describe
the same thing — the state of every node the run has touched — so they are built
here once. Adding a field to a frame means adding it in this module only.
"""

from __future__ import annotations

from typing import Any

from durin.workflow.spec import node_description, node_label

# Argument keys that name what a tool acted on, in priority order. Mirrors the
# order the web UI uses to summarize a tool call, so the same call reads the same
# way in a node frame and in a chat tool block.
_TARGET_KEYS = (
    "path", "file_path", "filename", "image_path", "audio_path", "command",
    "url", "query", "pattern", "question", "name", "uri", "ref", "goal",
    "action", "source",
)
_TARGET_MAX = 120


def tool_target(arguments: dict | None) -> str | None:
    """The thing a tool call acted on: a path, a command, a query.

    Returned raw, never composed into a sentence — each surface renders the
    phrase in the viewer's language, so a pre-composed string here would freeze
    one locale into the wire format.
    """
    for key in _TARGET_KEYS:
        value = (arguments or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_TARGET_MAX]
    return None


def _label(workflow: Any, node_id: str) -> str:
    node = getattr(workflow, "nodes", {}).get(node_id)
    return node_label(node) if node is not None else node_id


def _description(workflow: Any, node_id: str) -> str:
    node = getattr(workflow, "nodes", {}).get(node_id)
    return node_description(node) if node is not None else ""


def _finished_status(status: str) -> str:
    return "failed" if status in ("node_failed", "persist_failed") else "done"


def finished_frames(workflow: Any, runs: list[Any]) -> list[dict]:
    """One frame per accumulated ``NodeRun``, in visit order."""
    return [
        {
            "id": r.node_id,
            "label": _label(workflow, r.node_id),
            "description": _description(workflow, r.node_id),
            "status": _finished_status(r.status),
            "route_label": getattr(r, "route_label", None),
            "iteration": r.iteration,
            "budget": getattr(r, "budget", None),
        }
        for r in runs
    ]


def pending_frames(workflow: Any, from_node_id: str) -> list[dict]:
    """The nodes certain to run after ``from_node_id``, greyed in every surface.

    Walks forward only while each node has exactly one successor. A routing node
    ends the walk: which branch it takes is not known until it runs, so listing
    its targets would show a path that may never happen. Loops end the walk on
    revisit — a node already listed is not listed twice.
    """
    def _pending(node: Any) -> dict:
        return {"id": node.id, "label": node_label(node), "status": "pending",
                "route_label": None, "iteration": None, "budget": None}

    frames: list[dict] = []
    seen = {from_node_id}
    current = getattr(workflow.nodes.get(from_node_id), "next", None)
    while current and current in workflow.nodes and current not in seen:
        node = workflow.nodes[current]
        seen.add(current)
        frames.append(_pending(node))
        # A routing node ends the walk: which branch it takes is unknown until
        # it runs, so it is the last node that is certain to be visited. The
        # ``routes`` property is the node's own definition of that condition
        # (binary on_pass/on_fail or multi-way cases) — reusing it here means
        # this check cannot drift out of sync with what actually routes.
        if getattr(node, "routes", False):
            break
        current = getattr(node, "next", None)
    return frames


def running_frame(node: Any, *, iteration: int, budget: int | None,
                  started_at: float | None = None,
                  activity: dict | None = None,
                  round_: int | None = None,
                  max_rounds: int | None = None) -> dict:
    """The frame for the node the engine is about to execute.

    ``started_at`` is wall-clock epoch seconds; surfaces derive the elapsed
    clock from it rather than counting frames, so the clock stays right across
    a reconnect that misses frames.

    ``activity`` is what the node is doing right now — ``{tool, target, at}``,
    reported from inside the running turn — and ``round_`` which tool round it
    is on. ``max_rounds`` is the round budget to render ``round_`` against — the
    node's effective max_turns, a different axis from ``budget`` above (which is
    the node's *visit* budget: how many times the graph may re-enter it). All
    three are None until the node reports, and stay None for a node type that
    has no rounds.
    """
    return {
        "id": node.id,
        "label": node_label(node),
        "description": node_description(node),
        "status": "running",
        "route_label": None,
        "iteration": iteration,
        "budget": budget,
        "started_at": started_at,
        "activity": activity,
        "round": round_,
        "max_rounds": max_rounds,
    }
