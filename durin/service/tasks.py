"""Service: a unified per-chat list of background tasks (sub-agents + workflow runs).

Read-only. Merges the in-memory sub-agent statuses (SubagentManager, scoped to the
chat session) with the on-disk workflow run manifests (run_log) into one list the
web UI's Tasks tray renders. Sub-agent timestamps are time.monotonic(); they are
converted to wall-clock here so they sort against the workflow manifests' time.time().
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Query, Result


def _iso_to_epoch(s: str | None) -> float:
    """Parse an ISO 8601 string (with or without timezone) to a UTC epoch float.

    Returns 0.0 on None or any parse error so missing timestamps sort to the bottom.
    """
    if not s:
        return 0.0
    try:
        # Python 3.11+ handles Z; older versions need manual replacement.
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
    # ("completed" | "needs_input" | "exhausted" | "aborted"), and "crashed" (reconcile).
    if status == "completed":
        return "done"
    if status == "needs_input":
        return "needs_input"
    if status == "running":
        return "running"
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


class BackgroundTask(Result):
    kind: str  # "subagent" | "workflow"
    id: str
    label: str
    status: str  # "running" | "needs_input" | "done" | "failed"
    started_at: float  # wall-clock epoch seconds
    ended_at: float | None
    session_key: str | None  # for drill-in into the chat thread view
    nodes: list[dict] | None = None  # workflow node tree; None for sub-agents
    task: str | None = None  # workflow run task (the input given to this run); None for sub-agents


class TasksListQuery(Query):
    session: str


class TasksListResult(Result):
    tasks: list[BackgroundTask]


class TasksService:
    def __init__(self, *, workspace: Any, subagent_manager: Any | None = None, sessions: Any | None = None) -> None:
        self._workspace = workspace
        self._subagents = subagent_manager
        self._sessions = sessions

    @route(
        "GET",
        "/api/v1/tasks",
        scope=Scope.SESSIONS_READ.value,
        request_model=TasksListQuery,
        response_model=TasksListResult,
        summary="List background tasks (sub-agents + workflow runs) for a chat session",
    )
    async def list(self, query: TasksListQuery, principal: Principal) -> TasksListResult:
        principal.require(Scope.SESSIONS_READ)
        tasks: list[BackgroundTask] = []

        if self._subagents is not None:
            wall, mono = time.time(), time.monotonic()
            for s in self._subagents.list_for_session(query.session):
                started = wall - (mono - s.started_at)
                ended = (wall - (mono - s.ended_at)) if s.ended_at is not None else None
                tasks.append(BackgroundTask(
                    kind="subagent", id=s.task_id, label=s.label,
                    status=_subagent_status(s.phase),
                    started_at=started, ended_at=ended, session_key=s.session_key,
                ))

        from durin.workflow import run_log
        from durin.workflow.loader import WorkflowNotFound, load_workflow
        from durin.workflow.spec import WorkflowError, node_label
        for rec in run_log.runs_for_session(self._workspace, query.session):
            node_runs = rec.get("runs") or []
            drill = node_runs[-1].get("session_key") if node_runs else None  # last node's session for drill-in; None for routing nodes that persist no session
            # Build a label map from the workflow definition. Best-effort: if the
            # definition file is missing or malformed, nodes fall back to prettified ids.
            label_map: dict[str, str] | None = None
            wf_name = rec.get("workflow", "")
            if wf_name:
                try:
                    wf_def = load_workflow(self._workspace, wf_name)
                    label_map = {nid: node_label(node) for nid, node in wf_def.nodes.items()}
                except (WorkflowNotFound, WorkflowError, Exception):
                    pass
            tasks.append(BackgroundTask(
                kind="workflow", id=rec.get("run_id", ""),
                label=wf_name,
                status=_workflow_status(rec.get("status", "")),
                started_at=float(rec.get("started_at") or 0.0),
                ended_at=rec.get("finished_at"),
                session_key=drill,
                nodes=_node_tree(node_runs, label_map),
                task=rec.get("task"),
            ))

        # Reconstruct finished sub-agents from persisted session lineage so the tray
        # history survives a gateway restart (the LRU is in-memory; children_of reads
        # line-0 metadata from disk). The LRU takes precedence: if a sub-agent is already
        # listed (running or recently finished), skip the persisted entry.
        if self._sessions is not None and hasattr(self._sessions, "children_of"):
            seen = {t.id for t in tasks if t.kind == "subagent"}
            for c in self._sessions.children_of(query.session):
                if c.get("origin_type") != "subagent":
                    continue
                tid = c.get("origin_id")
                if not tid or tid in seen:
                    continue  # LRU (running/recent) already has it
                title = c.get("title") or ""
                label = title.split(":", 1)[1].strip() if ":" in title else (title or tid)
                tasks.append(BackgroundTask(
                    kind="subagent", id=tid, label=label, status="done",
                    started_at=_iso_to_epoch(c.get("created_at")),
                    ended_at=None, session_key=c.get("key"),
                ))

        tasks.sort(key=lambda t: t.started_at, reverse=True)
        return TasksListResult(tasks=tasks)
