"""The ``tasks`` tool — one surface to observe, cancel and retry background work.

After launching work in the background (``spawn`` a sub-agent, ``run_workflow``,
or an ingest that needs more OCR than the inline budget allows), the agent
reaches for this single tool — regardless of work type — to see what it
launched and how it is going, and to cancel it. It mirrors the
``BackgroundTask`` list the web UI's Tasks tray renders (``GET /api/v1/tasks``):
both read the same merged view via :func:`durin.agent.background_tasks.collect_tasks`.

Actions:

- ``list``   — every sub-agent, workflow run and job in this session (running +
  recent).
- ``status`` — detail for one by id: a sub-agent's phase/iteration/tool calls, a
  workflow run's per-node tree and final output, or a job's page progress. The
  id is resolved across all three kinds, so the caller does not say which kind
  it is.
- ``stop``   — cancel one by id: a sub-agent via the manager (immediate), a
  workflow run via the cooperative cancel flag, a job via the registry (a job
  stops at its next page boundary). A workflow stop is graceful by default: the
  run stops at its next node boundary (a running script is still killed; an
  in-flight agent node finishes first). ``force=true`` — or a repeat ``stop`` on
  a run already cancelling — escalates to hard: the in-flight agent node is
  interrupted too.
- ``retry``  — requeue one failed or cancelled job by id (jobs only: a sub-agent
  or workflow run is redone by launching a new one). The row returns to
  ``queued`` with its finished pages kept, and a worker is launched for it; it
  resumes from the first missing page.

Scoped to ``core`` (the main agent). Sub-agents do not introspect each other.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from durin.agent.background_tasks import collect_tasks
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema

_MAX_TOOL_HISTORY = 8
_MAX_FINAL_PREVIEW = 4000
_MAX_WORK_DIR_FILES = 20


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
            "one by id) | stop (cancel one by id) | retry (requeue a failed or "
            "cancelled job by id).",
            enum=["list", "status", "stop", "retry"],
        ),
        id=StringSchema(
            description=(
                "The task id (a sub-agent id, a workflow run id, or a job id) "
                "— required for status, stop, and retry."
            ),
            min_length=1, max_length=64, nullable=True,
        ),
        force=BooleanSchema(
            description=(
                "stop only, workflow runs only: interrupt the node currently "
                "executing instead of letting it finish (default false = the run "
                "stops at its next node boundary). A repeat stop on a run that is "
                "already cancelling escalates to this automatically."
            ),
            nullable=True,
        ),
        required=["action"],
    )
)
class TasksTool(Tool, ContextAware):
    """Observe and cancel background work (sub-agents + workflow runs + jobs) in this session."""

    _scopes = {"core"}

    def __init__(
        self, workspace: str, subagent_manager: Any | None, sessions: Any | None,
        jobs: Any | None = None,
    ) -> None:
        self._workspace = workspace
        self._manager = subagent_manager
        self._sessions = sessions
        self._jobs = jobs
        self._request_ctx: RequestContext | None = None

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "workspace", None) is not None

    @classmethod
    def create(cls, ctx: Any) -> "TasksTool":
        # A JobRegistry is a stateless database handle, not live in-memory
        # state like subagent_manager/sessions — a fresh instance here is
        # equivalent to reusing one, since it just opens its own connection
        # to the same jobs.db every other JobRegistry in the process reads.
        from durin.jobs.registry import JobRegistry

        return cls(
            workspace=ctx.workspace,
            subagent_manager=getattr(ctx, "subagent_manager", None),
            sessions=getattr(ctx, "sessions", None),
            jobs=JobRegistry(),
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
            "sub-agents (spawn), workflow runs (run_workflow), and jobs (a large "
            "scanned document ingested for background OCR), in one place. "
            "action=list shows everything running or recently finished; "
            "action=status with an id gives detail (a sub-agent's progress, a "
            "workflow run's per-node tree, work dir, current files and output, "
            "or a job's page progress); action=stop with an id cancels one "
            "(best-effort — a workflow run stops at its next node boundary, "
            "with force=true, or a repeat stop, interrupting the node already "
            "executing); action=retry with an id requeues a failed or "
            "cancelled job and starts a worker for it, resuming from the "
            "pages already transcribed (jobs only — redo a sub-agent or "
            "workflow by launching a new one). "
            "Use it when the user asks how the work is going, "
            "when you need a mid-run look at a workflow's files, or before "
            "stopping something. A finished sub-agent or workflow run arrives "
            "on its own as a follow-up message — do not loop sleep+status "
            "waiting for one; end your turn instead. A job pushes nothing: "
            "nobody tells you when it finishes, so check back with "
            "action=status (or list) instead of expecting a message. A "
            "job's progress is also visible in the dashboard's work panel, "
            "at roughly 1-2 seconds per page."
        )

    def _rows(self, session_key: str) -> list[dict]:
        return collect_tasks(
            self._workspace, subagent_manager=self._manager,
            sessions=self._sessions, jobs=self._jobs, session_key=session_key,
        )

    async def execute(  # type: ignore[override]
        self, action: str | None = None, id: str | None = None,
        force: bool | None = None, **kwargs: Any,
    ) -> str:
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
            return await self._do_stop(session_key, id, force=bool(force))
        if action == "retry":
            if not id:
                return "Error: 'id' is required for retry."
            return self._do_retry(session_key, id)
        return f"Error: unknown action {action!r} (use list | status | stop | retry)."

    def _render_list(self, rows: list[dict]) -> str:
        if not rows:
            return "No background tasks (sub-agents, workflow runs, or jobs) in this session."
        # A run winding down under a pending cancel ("stopping") has not finished:
        # a node is still executing. Counting it as finished would say the
        # opposite of the truth on the one line the model reads every check.
        running = sum(1 for r in rows if r["status"] in ("running", "stopping"))
        # Counted apart from both buckets: a queued job has not started, so
        # folding it into "finished" (which is what "everything not running"
        # did) says the opposite of the truth. Named only when there is one --
        # a permanent "0 queued" is noise on a line read every check.
        queued = sum(1 for r in rows if r["status"] == "queued")
        counts = f"{running} running, " + (f"{queued} queued, " if queued else "")
        lines = [
            f"{len(rows)} background task(s) in this session "
            f"({counts}{len(rows) - running - queued} finished):"
        ]
        for r in rows:
            # Same honesty as the queued branch of the STATUS render: an age
            # beside a job reads as time spent working, and a queued job has
            # no worker at all. Blank the cell (width kept for alignment)
            # instead of printing a clock nobody is running against.
            if r["status"] == "queued":
                age_cell = " " * 10
            else:
                age_cell = f"age={_age_epoch(r['started_at'], r.get('ended_at')):<6}"
            lines.append(
                f"  [{r['id']}] {r['kind']:<8} {r['status']:<10} {age_cell} {r['label']}"
            )
        return "\n".join(lines)

    def _heal_orphaned_workflow(self, row: dict) -> str | None:
        """Repair a "running" workflow row whose owning process died.

        The manifest survives a gateway crash with status "running" (the
        2026-07-18 ghost); when the user pokes it before the periodic sweep
        does, flip it here and answer with the truth instead of describing a
        process that no longer exists. Returns the healed message, or None
        when the row is genuinely alive."""
        if row["kind"] != "workflow" or row["status"] != "running":
            return None
        from durin.workflow import run_log
        if not run_log.reconcile_one(self._workspace, row["label"], row["id"]):
            return None
        row["status"] = "crashed"
        return (
            f"Workflow run [{row['id']}] was still marked running, but the "
            "process that owned it is gone (a gateway restart or crash killed "
            "it mid-run). Marked it crashed; its partial trace is preserved. "
            "Re-run the workflow if the result is still needed."
        )

    def _render_status(self, session_key: str, task_id: str) -> str:
        row = next((r for r in self._rows(session_key) if r["id"] == task_id), None)
        if row is None:
            return f"Error: unknown task id {task_id!r} in this session."
        if row["kind"] == "subagent":
            return self._render_subagent_status(session_key, task_id, row)
        if row["kind"] == "job":
            return self._render_job_status(row)
        healed = self._heal_orphaned_workflow(row)
        if healed:
            return healed
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

    def _render_job_status(self, row: dict) -> str:
        out = [
            f"Job [{row['id']}] — {row['label']}",
            f"  status: {row['status']}",
        ]
        if row["status"] == "queued":
            # No age line: an age next to a job reads as time spent working,
            # and a queued job has no worker at all. It is waiting for the one
            # OCR slot (durin/jobs/spawn.py's MAX_CONCURRENT_OCR_JOBS), which
            # the job holding it frees when it finishes.
            out.append("  waiting for the OCR slot; no worker has started it yet")
        else:
            out.append(f"  age:    {_age_epoch(row['started_at'], row.get('ended_at'))}")
        total = row.get("units_total")
        if total is not None:
            out.append(f"  progress: {row.get('units_done') or 0}/{total}")
        # The worker's own reason, and the only place anyone can read it: its
        # stderr is inherited from the gateway daemon's boot log, which is
        # truncated on every start and not served by the log reader.
        error = row.get("error")
        if error:
            out.append(f"  error:  {error}")
        # Recovery advice lives here in the render, never inside the stored
        # error string: that string is shown verbatim to humans in the webui
        # tray, where tool syntax like "action=retry" is noise, while this
        # render is read only by the model.
        if row["status"] in ("failed", "cancelled"):
            out.append(
                "  recovery: action=retry requeues this job and resumes from "
                "the pages already transcribed; re-ingesting the document "
                "also recovers it."
            )
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
        work_dir = manifest.get("work_dir")
        if work_dir:
            out.append(f"  work dir: {work_dir}")
        if row.get("task"):
            task = row["task"]
            out.append(f"  task:   {task if len(task) <= 200 else task[:197] + '...'}")
        # Durations come from the manifest trace; the merged row's node summary has
        # no timing and collapses loop passes to one entry, so show each node's
        # LATEST pass duration (walk order: the last row for that id wins).
        durations: dict[str, float] = {}
        for r in manifest.get("runs") or []:
            if r.get("duration_s") is not None:
                durations[r.get("node_id")] = r["duration_s"]
        nodes = row.get("nodes") or []
        if nodes:
            out.append("  nodes:")
            for n in nodes:
                line = f"    - {n['id']} [{n['status']}] {n.get('label', '')}".rstrip()
                d = durations.get(n["id"])
                if d is not None:
                    line += f" ({d}s)"
                out.append(line)
        out.extend(self._work_dir_files(work_dir))
        missing = manifest.get("missing_artifacts") or []
        if missing:
            out.append("  declared artifacts not produced: " + ", ".join(missing))
        final = manifest.get("final_output")
        if final:
            if len(final) > _MAX_FINAL_PREVIEW:
                final = final[:_MAX_FINAL_PREVIEW].rstrip() + "\n… (truncated)"
            out.append(f"  final output:\n{final}")
        return "\n".join(out)

    @staticmethod
    def _work_dir_files(work_dir: str | None) -> list[str]:
        """Render the run's working-folder contents (relative paths + sizes, capped) —
        the mid-run window onto a workflow's artifacts as they appear."""
        if not work_dir or not Path(work_dir).is_dir():
            return []
        try:
            files = sorted(p for p in Path(work_dir).rglob("*") if p.is_file())
        except OSError:
            return []
        if not files:
            return []
        out = [f"  files in work dir ({len(files)}):"]
        for p in files[:_MAX_WORK_DIR_FILES]:
            try:
                out.append(f"    - {p.relative_to(work_dir)} ({p.stat().st_size:,} B)")
            except OSError:
                continue
        if len(files) > _MAX_WORK_DIR_FILES:
            out.append(f"    … and {len(files) - _MAX_WORK_DIR_FILES} more")
        return out

    async def _do_stop(self, session_key: str, task_id: str, *, force: bool = False) -> str:
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
        if row["kind"] == "job":
            if row["status"] == "queued":
                # The cleanest cancel there is: nothing has claimed the row,
                # and claim()'s UPDATE only ever moves a row out of "queued",
                # so no worker can pick it up afterwards. No page boundary to
                # wait for either -- no page has been read.
                self._jobs.cancel(task_id)
                return (
                    f"Job [{task_id}] cancelled while it was still waiting for "
                    "the OCR slot; no worker will pick it up."
                )
            if row["status"] != "running":
                return f"Job [{task_id}] is already {row['status']} — nothing to cancel."
            self._jobs.cancel(task_id)
            return (
                f"Job [{task_id}] asked to cancel. It stops at its next page "
                "boundary (a page already being transcribed finishes first). "
                "Nothing pushes a message when it does — check action=status "
                "if you need to confirm it stopped."
            )
        # workflow
        healed = self._heal_orphaned_workflow(row)
        if healed:
            return healed
        if row["status"] not in ("running", "stopping"):
            return f"Workflow run [{task_id}] is already {row['status']} — nothing to cancel."
        from durin.workflow.cancellation import is_cancelled, request_cancel
        # A repeat stop on a run already cancelling escalates: the caller asked
        # once, the run is still going — "stop it" now means "stop it NOW".
        hard = force or is_cancelled(task_id)
        request_cancel(task_id, hard=hard)
        if hard:
            return (
                f"Workflow run [{task_id}] force-stopped: the node currently "
                "executing is being interrupted. Its result still arrives as a "
                "follow-up, with status 'cancelled'."
            )
        return (
            f"Workflow run [{task_id}] asked to cancel. It stops at its next node "
            "boundary (a running script is killed; an agent node already executing "
            "finishes first — repeat the stop, or use force=true, to interrupt it). "
            "Its result still arrives as a follow-up, with status 'cancelled'."
        )

    @staticmethod
    def _retry_refusal(task_id: str, status: str) -> str:
        """The one wording for "this row cannot be retried right now", shared
        by the pre-check and the raced-requeue re-check so the two cannot
        drift apart."""
        text = f"Job [{task_id}] is {status} — only a failed or cancelled job can be retried."
        if status == "done":
            # A done job's transcription is on disk and indexed; a re-ingest
            # of the same document short-circuits on that sidecar and returns
            # it without redoing any work — promise that outcome, not a job.
            text += (
                " Its transcription is finished and already in the Library; "
                "ingesting the document again returns it."
            )
        return text

    def _do_retry(self, session_key: str, task_id: str) -> str:
        row = next((r for r in self._rows(session_key) if r["id"] == task_id), None)
        if row is None:
            return f"Error: unknown task id {task_id!r} in this session."
        if row["kind"] != "job":
            return (
                f"Error: retry only applies to background jobs — [{task_id}] "
                f"is a {row['kind']}."
            )
        if row["status"] not in ("failed", "cancelled"):
            return self._retry_refusal(task_id, row["status"])
        if not self._jobs.requeue(task_id):
            # The row moved between the read above and the guarded UPDATE
            # (another retry landed, or the row was pruned). Never launch a
            # worker after a requeue that wrote nothing — answer with the
            # row as it is now instead.
            fresh = self._jobs.get(task_id)
            if fresh is None:
                return f"Error: unknown task id {task_id!r} in this session."
            return self._retry_refusal(task_id, fresh.status)
        from durin.jobs.spawn import respawn

        job = self._jobs.get(task_id)
        respawn(job)
        # "Queued", never "running": under the OCR cap the launched worker's
        # claim is refused while another job holds the slot, and the running
        # worker's finish-time chain (or the periodic sweep) picks this row
        # up when the slot frees.
        return (
            f"Job [{task_id}] requeued. Progress is preserved — "
            f"{job.units_done}/{job.units_total} pages already transcribed — "
            "and the worker resumes from the first missing page. It starts "
            "when the OCR slot frees (immediately if it is free; a missed "
            "launch is picked up by the periodic sweep within about a "
            "minute). Nothing "
            "pushes a message when it finishes — check action=status (or "
            "list) to follow it."
        )
