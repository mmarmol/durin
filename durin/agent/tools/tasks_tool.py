"""The ``tasks`` tool — one surface to observe and cancel background work.

After launching work in the background (``spawn`` a sub-agent or
``run_workflow``), the agent reaches for this single tool — regardless of work
type — to see what it launched and how it is going, and to cancel it. It mirrors
the ``BackgroundTask`` list the web UI's Tasks tray renders (``GET /api/v1/tasks``):
both read the same merged view via :func:`durin.agent.background_tasks.collect_tasks`.

Actions:

- ``list``   — every sub-agent and workflow run in this session (running + recent).
- ``status`` — detail for one by id: a sub-agent's phase/iteration/tool calls, or
  a workflow run's per-node tree and final output. The id is resolved across both
  kinds, so the caller does not say which kind it is.
- ``stop``   — cancel one by id: a sub-agent via the manager, a workflow run via
  the cooperative cancel flag (it stops between nodes — best-effort).

Scoped to ``core`` (the main agent). Sub-agents do not introspect each other.
"""

from __future__ import annotations

import time
from typing import Any

from durin.agent.background_tasks import collect_tasks
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

_MAX_TOOL_HISTORY = 8
_MAX_FINAL_PREVIEW = 4000


def _age_epoch(started_at: float, ended_at: float | None) -> str:
    end = ended_at if ended_at is not None else time.time()
    age_s = max(0.0, end - started_at)
    if age_s < 60:
        return f"{age_s:.0f}s"
    if age_s < 3600:
        return f"{age_s / 60:.1f}m"
    return f"{age_s / 3600:.1f}h"


def _age_mono(started_at: float, ended_at: float | None) -> str:
    end = ended_at if ended_at is not None else time.monotonic()
    age_s = max(0.0, end - started_at)
    if age_s < 60:
        return f"{age_s:.1f}s"
    if age_s < 3600:
        return f"{age_s / 60:.1f}m"
    return f"{age_s / 3600:.1f}h"


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "What to do: list (all background work this session) | status (detail for "
            "one by id) | stop (cancel one by id).",
            enum=["list", "status", "stop"],
        ),
        id=StringSchema(
            description="The task id (a sub-agent id or a workflow run id) — required for status and stop.",
            min_length=1, max_length=64, nullable=True,
        ),
        required=["action"],
    )
)
class TasksTool(Tool, ContextAware):
    """Observe and cancel background work (sub-agents + workflow runs) in this session."""

    _scopes = {"core"}

    def __init__(self, workspace: str, subagent_manager: Any | None, sessions: Any | None) -> None:
        self._workspace = workspace
        self._manager = subagent_manager
        self._sessions = sessions
        self._request_ctx: RequestContext | None = None

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "workspace", None) is not None

    @classmethod
    def create(cls, ctx: Any) -> "TasksTool":
        return cls(
            workspace=ctx.workspace,
            subagent_manager=getattr(ctx, "subagent_manager", None),
            sessions=getattr(ctx, "sessions", None),
        )

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx = ctx

    def _session_key(self) -> str | None:
        ctx = self._request_ctx
        if ctx is None:
            return None
        if ctx.session_key:
            return ctx.session_key
        if ctx.channel and ctx.chat_id:
            return f"{ctx.channel}:{ctx.chat_id}"
        return None

    @property
    def name(self) -> str:
        return "tasks"

    @property
    def description(self) -> str:
        return (
            "Observe and cancel the background work you launched in this session — "
            "sub-agents (spawn) and workflow runs (run_workflow), in one place. "
            "action=list shows everything running or recently finished; "
            "action=status with an id gives detail (a sub-agent's progress, or a "
            "workflow run's per-node tree and output); action=stop with an id cancels "
            "one (best-effort). Use this instead of waiting when you want to check on "
            "or stop something you started. Poll with the sleep tool between checks; "
            "don't busy-loop."
        )

    def _rows(self, session_key: str) -> list[dict]:
        return collect_tasks(
            self._workspace, subagent_manager=self._manager,
            sessions=self._sessions, session_key=session_key,
        )

    async def execute(self, action: str | None = None, id: str | None = None, **kwargs: Any) -> str:  # type: ignore[override]
        session_key = self._session_key()
        if session_key is None:
            return "Error: no session context available for tasks."

        if action == "list":
            return self._render_list(self._rows(session_key))
        if action == "status":
            if not id:
                return "Error: 'id' is required for status."
            return self._render_status(session_key, id)
        if action == "stop":
            if not id:
                return "Error: 'id' is required for stop."
            return await self._do_stop(session_key, id)
        return f"Error: unknown action {action!r} (use list | status | stop)."

    def _render_list(self, rows: list[dict]) -> str:
        if not rows:
            return "No background tasks (sub-agents or workflow runs) in this session."
        running = sum(1 for r in rows if r["status"] == "running")
        lines = [
            f"{len(rows)} background task(s) in this session "
            f"({running} running, {len(rows) - running} finished):"
        ]
        for r in rows:
            age = _age_epoch(r["started_at"], r.get("ended_at"))
            lines.append(
                f"  [{r['id']}] {r['kind']:<8} {r['status']:<10} age={age:<6} {r['label']}"
            )
        return "\n".join(lines)

    def _render_status(self, session_key: str, task_id: str) -> str:
        row = next((r for r in self._rows(session_key) if r["id"] == task_id), None)
        if row is None:
            return f"Error: unknown task id {task_id!r} in this session."
        if row["kind"] == "subagent":
            return self._render_subagent_status(session_key, task_id, row)
        return self._render_workflow_status(row)

    def _render_subagent_status(self, session_key: str, task_id: str, row: dict) -> str:
        # Prefer the live manager snapshot (phase/iteration/tools/usage); fall back to
        # the merged row for a sub-agent reconstructed from persisted history.
        status = self._manager.get_status_for(task_id, session_key) if self._manager else None
        if status is None:
            return (
                f"Sub-agent [{task_id}] — {row['label']}\n"
                f"  status: {row['status']} (from history; no live detail available)"
            )
        is_running = self._manager._is_running(task_id)
        age = _age_mono(status.started_at, status.ended_at)
        out = [
            f"Sub-agent [{status.task_id}] — {status.label}",
            f"  status:    {row['status']}",
            f"  phase:     {status.phase}",
            f"  iteration: {status.iteration}",
            f"  age:       {age}",
        ]
        if status.usage:
            out.append("  usage:     " + ", ".join(f"{k}={v}" for k, v in sorted(status.usage.items())))
        if status.tool_events:
            tail = status.tool_events[-_MAX_TOOL_HISTORY:]
            out.append(f"  tool calls ({len(status.tool_events)} total, showing last {len(tail)}):")
            for ev in tail:
                if not isinstance(ev, dict):
                    continue
                detail = (ev.get("detail") or "").replace("\n", " ").strip()
                if len(detail) > 80:
                    detail = detail[:77] + "..."
                out.append(f"    - {ev.get('name', '?')} [{ev.get('status', '?')}] {detail}")
        if status.error:
            out.append(f"  error:     {status.error[:200]}")
        if not is_running and status.stop_reason:
            out.append(f"  stop:      {status.stop_reason}")
        return "\n".join(out)

    def _render_workflow_status(self, row: dict) -> str:
        from durin.workflow import run_log
        manifest = run_log.read_manifest(self._workspace, row["label"], row["id"]) or {}
        age = _age_epoch(row["started_at"], row.get("ended_at"))
        out = [
            f"Workflow run [{row['id']}] — {row['label']}",
            f"  status: {row['status']}",
            f"  age:    {age}",
        ]
        if row.get("task"):
            task = row["task"]
            out.append(f"  task:   {task if len(task) <= 200 else task[:197] + '...'}")
        nodes = row.get("nodes") or []
        if nodes:
            out.append("  nodes:")
            for n in nodes:
                out.append(f"    - {n['id']} [{n['status']}] {n.get('label', '')}".rstrip())
        final = manifest.get("final_output")
        if final:
            if len(final) > _MAX_FINAL_PREVIEW:
                final = final[:_MAX_FINAL_PREVIEW].rstrip() + "\n… (truncated)"
            out.append(f"  final output:\n{final}")
        return "\n".join(out)

    async def _do_stop(self, session_key: str, task_id: str) -> str:
        row = next((r for r in self._rows(session_key) if r["id"] == task_id), None)
        if row is None:
            return f"Error: unknown task id {task_id!r} in this session."
        if row["kind"] == "subagent":
            if self._manager is None:
                return f"Error: cannot stop sub-agent [{task_id}] — no sub-agent manager."
            outcome = await self._manager.stop_task(task_id, session_key)
            if outcome == "stopped":
                return f"Sub-agent [{task_id}] cancelled."
            if outcome == "not_running":
                return f"Sub-agent [{task_id}] had already finished — nothing to cancel."
            return f"Error: unknown sub-agent id {task_id!r} in this session."
        # workflow
        if row["status"] != "running":
            return f"Workflow run [{task_id}] is already {row['status']} — nothing to cancel."
        from durin.workflow.cancellation import request_cancel
        request_cancel(task_id)
        return (
            f"Workflow run [{task_id}] asked to cancel. It stops at its next node "
            "boundary (a node already executing finishes first); its result still "
            "arrives as a follow-up, with status 'cancelled'."
        )
