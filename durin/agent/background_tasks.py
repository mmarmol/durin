"""The merged view of a session's background work: sub-agents + workflow runs.

One place builds this list so the two consumers never diverge: the HTTP service
(``GET /api/v1/tasks``, which the web UI's Tasks tray renders) and the agent's
own ``tasks`` tool. It merges the in-memory sub-agent statuses (SubagentManager,
scoped to the session) with the on-disk workflow run manifests (run_log), plus
persisted sub-agent lineage so history survives a gateway restart.

Returns plain dicts with a stable shape — ``kind`` ("subagent" | "workflow"),
``id``, ``label``, ``status`` ("running" | "needs_input" | "done" | "failed" |
"cancelled"), ``started_at`` (wall-clock epoch), ``ended_at``, ``session_key``,
and for workflows a ``nodes`` tree and the run ``task``. The service wraps these
into its pydantic ``BackgroundTask`` response model; the tool renders them.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


def _iso_to_epoch(s: str | None) -> float:
    """Parse an ISO 8601 string (with or without timezone) to a UTC epoch float.

    Returns 0.0 on None or any parse error so missing timestamps sort to the bottom.
    """
    if not s:
        return 0.0
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _subagent_status(phase: str) -> str:
    if phase == "done":
        return "done"
    if phase in ("error", "cancelled"):
        return "failed"
    return "running"  # initializing | awaiting_tools | tools_completed | final_response


def _workflow_status(status: str) -> str:
    # Run-level statuses: "running" (run_log), WorkflowResult.status literals
    # ("completed" | "needs_input" | "exhausted" | "aborted" | "cancelled"), and
    # "crashed" (reconcile).
    if status == "completed":
        return "done"
    if status == "needs_input":
        return "needs_input"
    if status == "running":
        return "running"
    if status == "cancelled":
        return "cancelled"
    return "failed"  # exhausted | aborted | crashed


def _node_run_status(s: str) -> str:
    return "failed" if s in ("node_failed", "persist_failed") else "done"


def _node_tree(node_runs: list[dict], label_map: dict[str, str] | None = None) -> list[dict]:
    """Group manifest node runs by node id (first-seen order). A node id that
    recurs across iterations collapses to one entry showing its latest status.
    label_map maps node id → human label; absent ids fall back to a prettified id."""
    from durin.workflow.spec import _prettify_id

    order: list[str] = []
    latest: dict[str, dict] = {}
    for r in node_runs:
        nid = r.get("node_id") or ""
        if nid not in latest:
            order.append(nid)
        label = (label_map or {}).get(nid) or _prettify_id(nid)
        latest[nid] = {"id": nid, "label": label, "status": _node_run_status(r.get("status", "ok")), "branches": None}
    return [latest[nid] for nid in order]


def collect_tasks(
    workspace: Any, *, subagent_manager: Any | None = None,
    sessions: Any | None = None, session_key: str,
) -> list[dict]:
    """Merge sub-agents + workflow runs for one session, newest-first.

    ``workspace`` is the workspace path (for workflow manifests). ``subagent_manager``
    and ``sessions`` are optional — when absent, that source contributes nothing.
    """
    tasks: list[dict] = []

    if subagent_manager is not None:
        wall, mono = time.time(), time.monotonic()
        for s in subagent_manager.list_for_session(session_key):
            started = wall - (mono - s.started_at)
            ended = (wall - (mono - s.ended_at)) if s.ended_at is not None else None
            tasks.append({
                "kind": "subagent", "id": s.task_id, "label": s.label,
                "status": _subagent_status(s.phase),
                "started_at": started, "ended_at": ended, "session_key": s.session_key,
                "nodes": None, "task": None,
            })

    from durin.workflow import run_log
    from durin.workflow.loader import WorkflowNotFound, load_workflow
    from durin.workflow.spec import WorkflowError, node_label
    for rec in run_log.runs_for_session(workspace, session_key):
        node_runs = rec.get("runs") or []
        drill = node_runs[-1].get("session_key") if node_runs else None  # last node's session for drill-in
        label_map: dict[str, str] | None = None
        wf_name = rec.get("workflow", "")
        if wf_name:
            try:
                wf_def = load_workflow(workspace, wf_name)
                label_map = {nid: node_label(node) for nid, node in wf_def.nodes.items()}
            except (WorkflowNotFound, WorkflowError, Exception):
                pass
        tasks.append({
            "kind": "workflow", "id": rec.get("run_id", ""),
            "label": wf_name,
            "status": _workflow_status(rec.get("status", "")),
            "started_at": float(rec.get("started_at") or 0.0),
            "ended_at": rec.get("finished_at"),
            "session_key": drill,
            "nodes": _node_tree(node_runs, label_map),
            "task": rec.get("task"),
        })

    # Reconstruct finished sub-agents from persisted session lineage so the history
    # survives a gateway restart (the LRU is in-memory; children_of reads line-0
    # metadata from disk). The LRU takes precedence: skip any already listed.
    if sessions is not None and hasattr(sessions, "children_of"):
        seen = {t["id"] for t in tasks if t["kind"] == "subagent"}
        for c in sessions.children_of(session_key):
            if c.get("origin_type") != "subagent":
                continue
            tid = c.get("origin_id")
            if not tid or tid in seen:
                continue
            title = c.get("title") or ""
            label = title.split(":", 1)[1].strip() if ":" in title else (title or tid)
            tasks.append({
                "kind": "subagent", "id": tid, "label": label, "status": "done",
                "started_at": _iso_to_epoch(c.get("created_at")),
                "ended_at": None, "session_key": c.get("key"),
                "nodes": None, "task": None,
            })

    tasks.sort(key=lambda t: t["started_at"], reverse=True)
    return tasks
