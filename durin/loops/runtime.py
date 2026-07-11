"""Lifecycle interpreter: fires a loop's workflow, reads the terminal status,
verifies the goal, and decides done / no_goal / needs_operator / escalated.

Iterates on new information only — a fire happens because a trigger delivered
one (cron tick, manual/chat request); there is no timer-based blind retry."""

from __future__ import annotations

import uuid
from pathlib import Path

from durin.agent.tools._telemetry import emit_tool_event
from durin.loops import run_log
from durin.loops.checks import verify_goal
from durin.loops.spec import LoopSpec
from durin.loops.store import load_loop


class LoopBusy(Exception):
    """concurrency=single and an active run exists."""


class LoopsRuntime:
    def __init__(self, workspace, *, workflow_exec, judge, keep_runs: int,
                 check_timeout_s: int, on_operator_ask=None, run_id_factory=None):
        self._ws = Path(workspace)
        self._exec = workflow_exec
        self._judge = judge
        self._keep_runs = keep_runs
        self._timeout = check_timeout_s
        self._notify = on_operator_ask
        self._run_id = run_id_factory or (lambda: uuid.uuid4().hex[:12])

    async def fire(self, name: str, *, source: str, task: str | None = None) -> dict:
        spec = load_loop(self._ws, name)
        if spec.concurrency == "single" and run_log.active_runs(self._ws, name):
            raise LoopBusy(f"loop '{name}' already has an active run")
        return await self._run(spec, source=source, task=task)

    async def try_fire(self, name: str, *, source: str) -> dict | None:
        spec = load_loop(self._ws, name)
        if not spec.enabled:
            return None
        if spec.concurrency == "single" and run_log.active_runs(self._ws, name):
            emit_tool_event("loops.fired", {"loop": name, "source": source, "skipped": True})
            return None
        return await self._run(spec, source=source, task=None)

    async def answer(self, name: str, run_id: str, answer: str) -> dict:
        spec = load_loop(self._ws, name)
        record = run_log.read_run(self._ws, name, run_id)
        if not record or record.get("status") != "needs_operator":
            raise ValueError(f"run '{run_id}' of loop '{name}' is not awaiting an answer")
        run_log.update_run(self._ws, name, run_id, status="running")
        try:
            result = await self._exec(spec.workflow, answer, resume_run_id=record["workflow_run_id"])
        except Exception as exc:  # noqa: BLE001 — any failure ends the run honestly
            return await self._finish(spec, run_id, "error", None, detail=str(exc))
        return await self._interpret(spec, run_id, result)

    async def _run(self, spec: LoopSpec, *, source: str, task: str | None) -> dict:
        run_id = self._run_id()
        effective_task = task or spec.goal_intent
        run_log.start_run(self._ws, spec.name, run_id, source=source, task=effective_task)
        emit_tool_event("loops.fired", {"loop": spec.name, "source": source, "skipped": False})
        try:
            result = await self._exec(spec.workflow, effective_task, resume_run_id=None)
        except Exception as exc:  # noqa: BLE001
            return await self._finish(spec, run_id, "error", None, detail=str(exc))
        return await self._interpret(spec, run_id, result)

    async def _interpret(self, spec: LoopSpec, run_id: str, result) -> dict:
        wf_run_id = result.run_id or None
        if result.status == "needs_input":
            record = run_log.finalize_run(self._ws, spec.name, run_id, status="needs_operator",
                                          workflow_run_id=wf_run_id, ask=result.final_output or "")
            await self._say(spec, run_id, "ask", f"[{spec.name} · {run_id}] {record.get('ask') or ''}")
            return record
        if result.status == "completed":
            try:
                verdict = await verify_goal(spec, result.final_output or "", judge=self._judge,
                                            work_dir=result.output_dir, timeout_s=self._timeout)
            except Exception as exc:  # noqa: BLE001 — a judge failure must not strand the run
                return await self._finish(spec, run_id, "error", wf_run_id, detail=str(exc))
            status = "done" if verdict.reached else "no_goal"
            record = run_log.finalize_run(self._ws, spec.name, run_id, status=status,
                                          workflow_run_id=wf_run_id, checks=verdict.results,
                                          goal_reached=verdict.reached)
        elif result.status == "exhausted":
            record = run_log.finalize_run(self._ws, spec.name, run_id, status="no_goal",
                                          workflow_run_id=wf_run_id, goal_reached=False)
        else:  # aborted / cancelled
            record = run_log.finalize_run(self._ws, spec.name, run_id, status="error",
                                          workflow_run_id=wf_run_id, goal_reached=False)
        return await self._post_finish(spec, run_id, record)

    async def _finish(self, spec: LoopSpec, run_id: str, status: str, wf_run_id, detail: str = "") -> dict:
        record = run_log.finalize_run(self._ws, spec.name, run_id, status=status,
                                      workflow_run_id=wf_run_id, detail=detail or None, goal_reached=False)
        return await self._post_finish(spec, run_id, record)

    async def _post_finish(self, spec: LoopSpec, run_id: str, record: dict) -> dict:
        if record["status"] in ("no_goal", "error"):
            streak = run_log.consecutive_no_goal(self._ws, spec.name)
            if streak >= spec.stuck_after:
                record = run_log.update_run(self._ws, spec.name, run_id, status="escalated")
                emit_tool_event("loops.escalated", {"loop": spec.name, "run_id": run_id,
                                                    "consecutive_no_goal": streak})
                await self._say(spec, run_id, "escalation",
                                f"loop '{spec.name}' failed to reach its goal {streak} times in a row")
        emit_tool_event("loops.run_finished", {"loop": spec.name, "run_id": run_id,
                                               "status": record["status"],
                                               "goal_reached": bool(record.get("goal_reached"))})
        run_log.prune_runs(self._ws, spec.name, self._keep_runs)
        return record

    async def _say(self, spec: LoopSpec, run_id: str, kind: str, text: str) -> None:
        if self._notify:
            try:
                await self._notify(spec.name, run_id, kind, text)
            except Exception:  # noqa: BLE001 — notification is best-effort
                pass
