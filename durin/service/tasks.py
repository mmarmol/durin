"""Service: a unified per-chat list of background tasks (sub-agents + workflow runs).

Read-only. Merges the in-memory sub-agent statuses (SubagentManager, scoped to the
chat session) with the on-disk workflow run manifests (run_log) into one list the
web UI's Tasks tray renders. Sub-agent timestamps are time.monotonic(); they are
converted to wall-clock here so they sort against the workflow manifests' time.time().
"""

from __future__ import annotations

import time
from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Query, Result


def _subagent_status(phase: str) -> str:
    if phase == "done":
        return "done"
    if phase == "error":
        return "failed"
    return "running"


def _workflow_status(status: str) -> str:
    if status in ("completed", "ok"):
        return "done"
    if status == "needs_input":
        return "needs_input"
    if status == "running":
        return "running"
    return "failed"  # exhausted | aborted | crashed | anything terminal-but-not-ok


class BackgroundTask(Result):
    kind: str  # "subagent" | "workflow"
    id: str
    label: str
    status: str  # "running" | "needs_input" | "done" | "failed"
    started_at: float  # wall-clock epoch seconds
    ended_at: float | None
    session_key: str | None  # for drill-in into the chat thread view


class TasksListQuery(Query):
    session: str


class TasksListResult(Result):
    tasks: list[BackgroundTask]


class TasksService:
    def __init__(self, *, workspace: Any, subagent_manager: Any | None = None) -> None:
        self._workspace = workspace
        self._subagents = subagent_manager

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
        for rec in run_log.runs_for_session(self._workspace, query.session):
            node_runs = rec.get("runs") or []
            drill = node_runs[-1].get("session_key") if node_runs else None
            tasks.append(BackgroundTask(
                kind="workflow", id=rec.get("run_id", ""),
                label=rec.get("workflow", ""),
                status=_workflow_status(rec.get("status", "")),
                started_at=float(rec.get("started_at") or 0.0),
                ended_at=rec.get("finished_at"),
                session_key=drill,
            ))

        tasks.sort(key=lambda t: t.started_at, reverse=True)
        return TasksListResult(tasks=tasks)
