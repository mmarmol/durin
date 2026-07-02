"""Service: a unified per-chat list of background tasks (sub-agents + workflow runs).

Read-only. The merge of in-memory sub-agent statuses with on-disk workflow run
manifests lives in :mod:`durin.agent.background_tasks` so this HTTP surface and the
agent's own ``tasks`` tool render the same list; this module wraps those dicts into
the pydantic response model. ``_subagent_status`` / ``_workflow_status`` are
re-exported for callers (and tests) that import the status mapping from here.
"""

from __future__ import annotations

from typing import Any

from durin.agent.background_tasks import (
    _subagent_status,
    _workflow_status,
    collect_tasks,
)
from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Query, Result

__all__ = [
    "BackgroundTask", "TasksService", "TasksListQuery", "TasksListResult",
    "_subagent_status", "_workflow_status",
]


class BackgroundTask(Result):
    kind: str  # "subagent" | "workflow"
    id: str
    label: str
    status: str  # "running" | "needs_input" | "done" | "failed" | "cancelled"
    started_at: float  # wall-clock epoch seconds
    ended_at: float | None
    session_key: str | None  # for drill-in into the chat thread view
    nodes: list[dict] | None = None  # workflow node tree; None for sub-agents
    task: str | None = None  # workflow run task (the input given to this run); None for sub-agents
    needs_input_detail: str | None = None  # the gate's questions when status=="needs_input"; None otherwise


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
        rows = collect_tasks(
            self._workspace, subagent_manager=self._subagents,
            sessions=self._sessions, session_key=query.session,
        )
        return TasksListResult(tasks=[BackgroundTask(**r) for r in rows])
