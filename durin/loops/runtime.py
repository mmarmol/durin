"""Lifecycle interpreter: fires a loop's workflow, reads the terminal status,
verifies the goal, and decides done / no_goal / needs_operator / waiting_info /
escalated.

Iterates on new information only — a fire happens because a trigger delivered
one (cron tick, manual/chat request); there is no timer-based blind retry.

Audience tagging convention: a workflow's needs_input ask (``final_output``)
that starts with the literal tag ``[TO:counterpart]`` is directed at the
external party the loop is corresponding with, not at the operator. The tag
is stripped before the ask is stored or delivered. A tagged ask resolves
against the run's origin (the trigger context recorded at fire time via
``run_log.start_run``'s ``origin`` param): if ``origin["thread"]`` is set, the
run parks as ``waiting_info``, a claim is registered (thread key -> loop/run)
so a later inbound message on that thread can find its way back to this run,
and the question is handed to ``on_counterpart_ask`` for delivery. If there is
no origin thread (e.g. a cron or manual fire with nobody to reply to), the ask
degrades to the normal ``needs_operator`` lane with a note appended so the
question is never lost. An untagged ask is always operator-bound — exactly
V1 behavior."""

from __future__ import annotations

import uuid
from pathlib import Path

from durin.agent.tools._telemetry import emit_tool_event
from durin.loops import claims, run_log
from durin.loops.checks import verify_goal
from durin.loops.spec import LoopSpec
from durin.loops.store import load_loop
from durin.telemetry.logger import bind_telemetry, current_telemetry, get_session_logger, reset_telemetry

_COUNTERPART_TAG = "[TO:counterpart]"
_COUNTERPART_UNAVAILABLE_NOTE = " (counterpart channel unavailable — answer here)"


def _parse_ask(text: str) -> tuple[bool, str]:
    """Split a workflow ask into (is_counterpart_bound, stripped_text)."""
    if text.startswith(_COUNTERPART_TAG):
        return True, text[len(_COUNTERPART_TAG):].lstrip()
    return False, text


class LoopBusy(Exception):
    """concurrency=single and an active run exists."""


def _bind_loop_telemetry(name: str):
    """Bind a session telemetry logger for this loop dispatch.

    `fire`/`try_fire`/`answer` run outside an agent turn (cron dispatch,
    HTTP request) where AgentLoop never binds `current_telemetry()`, so
    `emit_tool_event` calls below would silently no-op. Bind a
    `loop:<name>` session logger for the duration of the call — unless a
    logger is already bound (e.g. `fire` invoked via the loops agent tool
    from inside a live agent turn), in which case leave the caller's
    binding alone so events keep flowing to the active session's file.
    Returns the reset token, or None if nothing was bound here.
    """
    if current_telemetry() is not None:
        return None
    return bind_telemetry(get_session_logger(f"loop:{name}"))


class LoopsRuntime:
    def __init__(self, workspace, *, workflow_exec, judge, keep_runs: int,
                 check_timeout_s: int, on_operator_ask=None, on_counterpart_ask=None,
                 run_id_factory=None):
        self._ws = Path(workspace)
        self._exec = workflow_exec
        self._judge = judge
        self._keep_runs = keep_runs
        self._timeout = check_timeout_s
        self._notify = on_operator_ask
        self._notify_counterpart = on_counterpart_ask
        self._run_id = run_id_factory or (lambda: uuid.uuid4().hex[:12])

    async def fire(self, name: str, *, source: str, task: str | None = None,
                    origin: dict | None = None) -> dict:
        token = _bind_loop_telemetry(name)
        try:
            spec = load_loop(self._ws, name)
            if spec.concurrency == "single" and run_log.active_runs(self._ws, name):
                raise LoopBusy(f"loop '{name}' already has an active run")
            return await self._run(spec, source=source, task=task, origin=origin)
        finally:
            if token is not None:
                reset_telemetry(token)

    async def try_fire(self, name: str, *, source: str, origin: dict | None = None) -> dict | None:
        token = _bind_loop_telemetry(name)
        try:
            spec = load_loop(self._ws, name)
            if not spec.enabled:
                return None
            if spec.concurrency == "single" and run_log.active_runs(self._ws, name):
                emit_tool_event("loops.fired", {"loop": name, "source": source, "skipped": True})
                return None
            return await self._run(spec, source=source, task=None, origin=origin)
        finally:
            if token is not None:
                reset_telemetry(token)

    async def answer(self, name: str, run_id: str, answer: str) -> dict:
        token = _bind_loop_telemetry(name)
        try:
            spec = load_loop(self._ws, name)
            record = run_log.read_run(self._ws, name, run_id)
            if not record or record.get("status") not in ("needs_operator", "waiting_info"):
                raise ValueError(f"run '{run_id}' of loop '{name}' is not awaiting an answer")
            run_log.update_run(self._ws, name, run_id, status="running")
            # Release now, before the resume: the old claim is stale the
            # moment the answer arrives (idempotent — a needs_operator run
            # held no claim). If _interpret re-asks another tagged question
            # below, it registers a fresh claim after this point, so a
            # trailing release here would wipe it and orphan the next
            # round-trip.
            claims.release_run(self._ws, name, run_id)
            try:
                result = await self._exec(spec.workflow, answer, resume_run_id=record["workflow_run_id"])
            except Exception as exc:  # noqa: BLE001 — any failure ends the run honestly
                return await self._finish(spec, run_id, "error", None, detail=str(exc))
            return await self._interpret(spec, run_id, result)
        finally:
            if token is not None:
                reset_telemetry(token)

    async def _run(self, spec: LoopSpec, *, source: str, task: str | None,
                    origin: dict | None = None) -> dict:
        run_id = self._run_id()
        effective_task = task or spec.goal_intent
        run_log.start_run(self._ws, spec.name, run_id, source=source, task=effective_task, origin=origin)
        emit_tool_event("loops.fired", {"loop": spec.name, "source": source, "skipped": False})
        try:
            result = await self._exec(spec.workflow, effective_task, resume_run_id=None)
        except Exception as exc:  # noqa: BLE001
            return await self._finish(spec, run_id, "error", None, detail=str(exc))
        return await self._interpret(spec, run_id, result)

    async def _interpret(self, spec: LoopSpec, run_id: str, result) -> dict:
        wf_run_id = result.run_id or None
        if result.status == "needs_input":
            is_counterpart, ask = _parse_ask(result.final_output or "")
            if is_counterpart:
                origin = (run_log.read_run(self._ws, spec.name, run_id) or {}).get("origin")
                thread = origin.get("thread") if isinstance(origin, dict) else None
                if thread:
                    record = run_log.finalize_run(self._ws, spec.name, run_id, status="waiting_info",
                                                  workflow_run_id=wf_run_id, ask=ask)
                    claims.register(self._ws, key=thread, loop=spec.name, run_id=run_id)
                    await self._say_counterpart(spec, run_id, origin, ask)
                    return record
                ask += _COUNTERPART_UNAVAILABLE_NOTE
            record = run_log.finalize_run(self._ws, spec.name, run_id, status="needs_operator",
                                          workflow_run_id=wf_run_id, ask=ask)
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

    async def _say_counterpart(self, spec: LoopSpec, run_id: str, origin: dict, text: str) -> None:
        if self._notify_counterpart:
            try:
                await self._notify_counterpart(spec.name, run_id, origin, text)
            except Exception:  # noqa: BLE001 — delivery is best-effort, mirrors _say
                pass
