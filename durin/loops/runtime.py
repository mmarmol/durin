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

import asyncio
import uuid
from pathlib import Path

from loguru import logger

from durin.agent.tools._telemetry import emit_tool_event
from durin.loops import claims, queue, run_log
from durin.loops.checks import verify_goal
from durin.loops.outcome import LoopOutcome, build_outcome
from durin.loops.spec import LoopNotFound, LoopSpec
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
                 run_id_factory=None, queue_ttl_s: int = 3600, on_outcome=None):
        self._ws = Path(workspace)
        self._exec = workflow_exec
        self._judge = judge
        self._keep_runs = keep_runs
        self._timeout = check_timeout_s
        self._notify = on_operator_ask
        self._notify_counterpart = on_counterpart_ask
        self._run_id = run_id_factory or (lambda: uuid.uuid4().hex[:12])
        self._queue_ttl_s = queue_ttl_s
        self._on_outcome = on_outcome
        # Strong references for sweep_orphans' backgrounded relaunches (see
        # asyncio's own warning: a task with no other reference can be
        # garbage-collected mid-flight). Discarded via the task's own
        # done-callback once it finishes.
        self._sweep_tasks: set[asyncio.Task] = set()

    def reserve_run_id(self) -> str:
        """Mint a run id a caller can report before the run exists.

        Lets a caller that is about to background the fire (e.g. the chat
        tool) hand the id back to the user immediately, then pass it into
        `fire` so the run that eventually starts uses the same id instead
        of minting a second one.
        """
        return self._run_id()

    async def fire(self, name: str, *, source: str, task: str | None = None,
                    origin: dict | None = None, run_id: str | None = None) -> dict:
        token = _bind_loop_telemetry(name)
        try:
            spec = load_loop(self._ws, name)
            if spec.concurrency == "single" and run_log.active_runs(self._ws, name):
                raise LoopBusy(f"loop '{name}' already has an active run")
            return await self._run(spec, source=source, task=task, origin=origin, run_id=run_id)
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
            # Re-stamp the owner, not just the status: THIS process is the one
            # executing the resume. A parked run is answered hours or days
            # later, routinely after a restart, so the recorded owner is
            # usually the long-dead process that first fired it. Leaving it
            # there makes the crash sweep read a live `running` run as an
            # orphan and finalize it `interrupted` while it is still going.
            from durin.utils.process_tree import process_identity

            run_log.update_run(self._ws, name, run_id, status="running",
                               owner=process_identity())
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

    async def sweep_orphans(self) -> list[str]:
        """Finalize runs whose process died, tell somebody, relaunch what never ran.

        A run killed with its process did not fail — it produced no result at
        all. Whether relaunching it is safe turns on one question: did any work
        happen? The existence of the workflow's own manifest answers it. The
        engine writes that manifest before it walks anything, so no manifest
        means the run never got going: the original cause is still unserved and
        a fresh run is the first attempt, not a retry.

        Existence, not a count of completed nodes, is the test. A node is
        recorded only when it finishes, so a run killed inside its first node
        shows an empty node list while being the most likely to have already
        posted somewhere external. Reading "no nodes" as safe would relaunch
        exactly those. Erring the other way costs one manual re-fire.

        Finalizing and notifying every orphan happens in one pass, BEFORE any
        relaunch starts. A relaunch runs a full replacement workflow (a slow
        LLM run is the common case); awaiting it inline here would stall every
        later orphan's own finalize/notify behind it, and — since a `single`-
        concurrency loop's manifest is still "running" until finalized — could
        even make an unrelated loop's orphan look busy to a concurrent fire
        attempt for the whole time. Each relaunch is instead started with
        `asyncio.create_task` once every orphan in this pass is already
        finalized and notified.

        The replacement's run id is nonetheless minted in the first pass, so
        the interrupted notice can name it: a loop that silently relaunches
        itself is its own class of problem. Minting is a local id, not a fire,
        so it costs the first pass nothing.

        A paused loop is never relaunched, and the notice says so rather than
        promising a replacement. `fire` deliberately ignores `enabled` (a
        manual run-now has to work on a paused loop), so this sweep is the
        only place that switch can be honoured for a run nobody asked for.
        """
        from durin.workflow import run_log as wf_run_log

        handled: list[str] = []
        to_relaunch: list[tuple[dict, str]] = []
        for rec in run_log.find_orphans(self._ws):
            loop_name = rec.get("loop") or ""
            run_id = rec.get("run_id") or ""
            if not loop_name or not run_id:
                continue
            try:
                spec = load_loop(self._ws, loop_name)
            except LoopNotFound:
                continue

            wf_run_id = rec.get("workflow_run_id")
            work_started = bool(
                wf_run_id
                and wf_run_log.read_manifest(self._ws, spec.workflow, wf_run_id) is not None
            )

            if work_started:
                detail = "interrupted by a restart with work already in flight"
                new_run_id = None
            elif not spec.enabled:
                # Paused while the gateway was down. Nothing ran, but firing a
                # replacement would run a loop its owner switched off — say so
                # instead, so the recipient isn't left expecting one.
                detail = ("interrupted by a restart before the workflow started; "
                          "not relaunched — the loop is paused")
                new_run_id = None
            else:
                new_run_id = self.reserve_run_id()
                detail = ("interrupted by a restart before the workflow started; "
                          f"relaunched as {new_run_id}")

            record = run_log.finalize_run(self._ws, loop_name, run_id,
                                          status="interrupted", workflow_run_id=wf_run_id,
                                          detail=detail, goal_reached=False,
                                          work_started=work_started)
            logger.info("loops: reconciled orphaned loop '{}' run {} as interrupted — {}",
                        loop_name, run_id, detail)
            await self._post_finish(spec, run_id, record)
            handled.append(run_id)

            if new_run_id is not None:
                to_relaunch.append((rec, new_run_id))

        for rec, new_run_id in to_relaunch:
            logger.info("loops: relaunching loop '{}' run {} as {} — no work had started",
                        rec.get("loop") or "", rec.get("run_id") or "", new_run_id)
            task = asyncio.create_task(self._relaunch_orphan(rec, new_run_id))
            self._sweep_tasks.add(task)
            task.add_done_callback(self._sweep_tasks.discard)
        return handled

    async def _relaunch_orphan(self, rec: dict, new_run_id: str) -> None:
        """Fire the replacement run for one orphan, backgrounded by sweep_orphans.

        Runs independently of the sweep pass that scheduled it, so nothing
        here can delay another orphan's finalize/notify or another loop's
        fire attempt. The id was already promised by the interrupted notice,
        so a replacement that does not happen is retracted to that same
        recipient rather than left as a log line nobody reads.
        """
        loop_name = rec.get("loop") or ""
        run_id = rec.get("run_id") or ""
        origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else None
        try:
            await self.fire(loop_name, source=rec.get("source") or "cron",
                            task=rec.get("task"), origin=origin, run_id=new_run_id)
        except LoopBusy:
            # A single-concurrency loop already has a live run — the cause is
            # being served, so a second run must not be stacked on it. The
            # promised replacement still never happened, and only the
            # recipient can judge whether the live run covers this cause.
            logger.info("loops: not relaunching loop '{}' run {} — already busy",
                        loop_name, run_id)
            await self.report_no_outcome(
                loop_name, new_run_id, origin=origin,
                reason=(f"the loop already had an active run when this replacement for "
                        f"run {run_id} was due to start"),
            )
        except Exception as exc:  # noqa: BLE001 — one bad orphan must not go unlogged
            logger.exception("loops: relaunch of loop '{}' run {} failed", loop_name, run_id)
            await self.report_no_outcome(
                loop_name, new_run_id, origin=origin,
                reason=f"this replacement for run {run_id} failed to start: {exc}",
            )

    async def report_no_outcome(self, loop: str, run_id: str, *,
                                origin: dict | None, reason: str) -> None:
        """Retract a run id that was announced before the run existed.

        Handing out a run id (a backgrounded chat fire, the replacement named
        in an interrupted notice) promises that an outcome follows. When the
        fire never happens, that promise breaks with nothing behind it but a
        log line — so the retraction goes to the same recipient the outcome
        would have reached. Carries `interrupted` because that is what
        happened as far as the recipient is concerned: no result was produced,
        and it is the routing status that reaches a standing destination as
        well as a session.
        """
        await self._deliver(LoopOutcome(
            loop=loop,
            run_id=run_id,
            status="interrupted",
            goal_reached=False,
            summary=f"Loop '{loop}' run {run_id} produced no outcome — {reason}.",
            origin=origin,
            workflow_run_id=None,
        ))

    async def _run(self, spec: LoopSpec, *, source: str, task: str | None,
                    origin: dict | None = None, run_id: str | None = None) -> dict:
        run_id = run_id or self._run_id()
        effective_task = task or spec.goal_intent
        run_log.start_run(self._ws, spec.name, run_id, source=source, task=effective_task, origin=origin)
        # Reserve the workflow's run id and persist it BEFORE launching: if this
        # process dies mid-run, the reserved id is the only handle a later sweep
        # has for asking whether the workflow ever started.
        wf_run_id = self._run_id()
        run_log.update_run(self._ws, spec.name, run_id, workflow_run_id=wf_run_id)
        emit_tool_event("loops.fired", {"loop": spec.name, "source": source, "skipped": False})
        try:
            result = await self._exec(spec.workflow, effective_task,
                                      resume_run_id=None, run_id=wf_run_id)
        except Exception as exc:  # noqa: BLE001
            return await self._finish(spec, run_id, "error", wf_run_id, detail=str(exc))
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
        await self._emit_outcome(spec.name, record)
        # Housekeeping only, and it must stay contained from here on: a caller
        # of fire() (_relaunch_orphan, the chat tool's backgrounded fire) reads
        # an exception out of this method as "the fire failed" and retracts
        # the run with a "produced no outcome" message. The real outcome just
        # went out above, so letting a filesystem/lock error from pruning or
        # the queue drain escape would retract a run whose actual outcome the
        # recipient already has — two contradictory messages for one run.
        try:
            run_log.prune_runs(self._ws, spec.name, self._keep_runs)
            # The run that just finished held the only concurrency slot a
            # `single` loop allows; if a channel event piled up in the queue
            # while it ran, fire it now instead of waiting for the next inbound
            # message. Scheduled via create_task (never awaited here) so a slow
            # drained run can't delay returning this record to the caller.
            if spec.enabled and spec.concurrency == "single":
                event = queue.pop_fresh(self._ws, spec.name, self._queue_ttl_s)
                if event is not None:
                    origin = event.get("origin")
                    emit_tool_event("loops.event_matched", {
                        "loop": spec.name,
                        "source_channel": (origin or {}).get("channel", ""),
                        "action": "drained",
                    })
                    asyncio.create_task(self._drain_fire(spec.name,
                                                         task=event.get("content"), origin=origin))
        except Exception:  # noqa: BLE001 — contained; see comment above
            logger.exception(
                "loops: post-outcome housekeeping (prune/queue-drain) for loop '{}' run {} failed",
                spec.name, run_id,
            )
        return record

    async def _emit_outcome(self, loop: str, record: dict) -> None:
        """Report a terminal run to whoever should hear about it.

        Every terminal status goes through here — the recipient decides what
        is worth showing."""
        await self._deliver(build_outcome(loop, record))

    async def _deliver(self, outcome: LoopOutcome) -> None:
        """Hand one outcome to the surface's delivery callback.

        Best-effort like the ask paths: a delivery failure must not turn a
        finished run into a failed one."""
        if self._on_outcome is None:
            return
        try:
            await self._on_outcome(outcome)
        except Exception:  # noqa: BLE001 — delivery is best-effort
            logger.exception("loops: outcome delivery for loop '{}' failed", outcome.loop)

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

    async def _drain_fire(self, loop_name: str, task: str | None, origin: dict | None) -> None:
        """Fire a drained event, re-enqueueing and logging if the loop is busy.

        Called via create_task from _post_finish, so unhandled exceptions
        are logged by asyncio as task exceptions. On LoopBusy, push the event
        BACK via queue.push and log a warning (mirrors how matcher._fire
        handles the race). Other exceptions are logged (run-level failures are
        already finalized by fire itself).
        """
        try:
            await self.fire(loop_name, source="channel", task=task, origin=origin)
        except LoopBusy:
            queue.push(self._ws, loop_name, {"content": task, "origin": origin})
            logger.warning(
                "loops: drained event for loop '{}' lost the fire race (now busy); "
                "re-queued for next available slot", loop_name
            )
        except Exception:  # noqa: BLE001 — run-level errors already finalized by fire
            logger.exception("loops: drained event for loop '{}' raised an unhandled exception", loop_name)
