"""Dispatcher: fires an automation's workflow, classifies the outcome, applies
delivery policy and the life condition, routes help asks, and dispatches
chains.

Iterates on new information only — a fire happens because a trigger delivered
one (cron tick, channel/webhook match, chain outcome, manual/chat request);
there is no timer-based blind retry (insistence, when configured, comes from
the trigger's own schedule plus the life condition — see
``durin.automations.spec.Life``).

Audience tagging convention: a workflow's needs_input ask (``final_output``)
that starts with the literal tag ``[TO:counterpart]`` is directed at the
external party the automation is corresponding with, not at the operator. The
tag is stripped before the ask is stored or delivered. A tagged ask resolves
against the run's origin (the trigger context recorded at fire time via
``run_log.start_run``'s ``origin`` param): if ``origin["thread"]`` is set, the
run parks as ``paused``, a claim is registered (thread key -> automation/run)
so a later inbound message on that thread can find its way back to this run,
and the question is handed to ``on_counterpart_ask`` for delivery. If there is
no origin thread (e.g. a cron or manual fire with nobody to reply to), the ask
degrades to the normal help lane with a note appended so the question is never
lost. An untagged ask is always operator-bound. Approval asks (a
``WorkNode.approval`` pause) never carry the counterpart tag in practice but
are parsed the same way regardless.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from pathlib import Path

from loguru import logger

from durin.agent.tools._telemetry import emit_tool_event
from durin.automations import claims, queue, run_log
from durin.automations.chains import CHAIN_HOP_CAP, chain_targets
from durin.automations.classify import classify, should_deliver
from durin.automations.outcome import AutomationOutcome, build_outcome, route
from durin.automations.spec import AutomationNotFound, AutomationSpec
from durin.automations.store import load_automation, save_automation
from durin.telemetry.logger import (
    bind_telemetry,
    current_telemetry,
    get_session_logger,
    reset_telemetry,
)
from durin.workflow.approval import parse_approval_reply
from durin.workflow.result import WorkflowResult

_COUNTERPART_TAG = "[TO:counterpart]"
_COUNTERPART_UNAVAILABLE_NOTE = " (counterpart channel unavailable — answer here)"


def _parse_ask(text: str) -> tuple[bool, str]:
    """Split a workflow ask into (is_counterpart_bound, stripped_text)."""
    if text.startswith(_COUNTERPART_TAG):
        return True, text[len(_COUNTERPART_TAG):].lstrip()
    return False, text


def _now_ms() -> int:
    return int(time.time() * 1000)


def _bind_automations_telemetry(name: str):
    """Bind a session telemetry logger for this automation dispatch.

    `fire`/`try_fire`/`answer` run outside an agent turn (cron dispatch, HTTP
    request) where AgentLoop never binds `current_telemetry()`, so
    `emit_tool_event` calls below would silently no-op. Bind an
    `automation:<name>` session logger for the duration of the call — unless
    a logger is already bound (e.g. `fire` invoked from inside a live agent
    turn), in which case leave the caller's binding alone so events keep
    flowing to the active session's file. Returns the reset token, or None
    if nothing was bound here.
    """
    if current_telemetry() is not None:
        return None
    return bind_telemetry(get_session_logger(f"automation:{name}"))


class AutomationBusy(Exception):
    """concurrency=single and an active run exists."""


class AutomationsRuntime:
    def __init__(self, workspace, *, workflow_exec, keep_runs: int,
                 on_help_ask=None, on_counterpart_ask=None, on_outcome=None,
                 run_id_factory=None, queue_ttl_s: int = 3600,
                 is_shutting_down=None):
        self._ws = Path(workspace)
        self._exec = workflow_exec
        self._keep_runs = keep_runs
        self._help = on_help_ask
        self._notify_counterpart = on_counterpart_ask
        self._on_outcome = on_outcome
        self._run_id = run_id_factory or (lambda: uuid.uuid4().hex[:12])
        self._queue_ttl_s = queue_ttl_s
        # () -> bool, true once the gateway has begun a graceful shutdown.
        # Consulted only right before finalizing an aborted/cancelled result
        # that is NOT a deliberate approval rejection, to tell a workflow
        # cancelled BY that shutdown apart from one a user deliberately
        # stopped — both arrive here as the same WorkflowResult.status.
        self._is_shutting_down = is_shutting_down or (lambda: False)
        # Strong references for every backgrounded task this runtime starts
        # (sweep_orphans' relaunches, chain dispatch, single-concurrency
        # queue drain) — asyncio's own warning: a task with no other
        # reference can be garbage-collected mid-flight. Discarded via each
        # task's own done-callback once it finishes.
        self._bg_tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> asyncio.Task:
        """Background a coroutine with a strong reference, never awaited here.

        Every backgrounded call in this class goes through this one place so
        none of them can be silently garbage-collected mid-flight, and so an
        unhandled exception inside one is guaranteed to reach a logger
        instead of surfacing only as asyncio's own untraceable "Task
        exception was never retrieved" warning — each coroutine passed here
        is responsible for catching and logging its own failures (see
        `_relaunch_orphan`, `_chain_fire`, `_drain_fire`).
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def reserve_run_id(self) -> str:
        """Mint a run id a caller can report before the run exists.

        Lets a caller that is about to background the fire (e.g. a chat tool)
        hand the id back to the user immediately, then pass it into `fire` so
        the run that eventually starts uses the same id instead of minting a
        second one.
        """
        return self._run_id()

    async def fire(self, name: str, *, source: str, task: str | None = None,
                    origin: dict | None = None, run_id: str | None = None,
                    chain_depth: int = 0) -> dict:
        token = _bind_automations_telemetry(name)
        try:
            spec = load_automation(self._ws, name)
            if spec.concurrency == "single" and run_log.active_runs(self._ws, name):
                raise AutomationBusy(f"automation '{name}' already has an active run")
            return await self._run(spec, source=source, task=task, origin=origin,
                                    run_id=run_id, chain_depth=chain_depth)
        finally:
            if token is not None:
                reset_telemetry(token)

    async def try_fire(self, name: str, *, source: str, task: str | None = None,
                         origin: dict | None = None) -> dict | None:
        token = _bind_automations_telemetry(name)
        try:
            spec = load_automation(self._ws, name)
            if not spec.enabled:
                return None
            if spec.concurrency == "single" and run_log.active_runs(self._ws, name):
                emit_tool_event("automations.fired", {"automation": name, "source": source, "skipped": True})
                return None
            return await self._run(spec, source=source, task=task, origin=origin)
        finally:
            if token is not None:
                reset_telemetry(token)

    async def answer(self, name: str, run_id: str, text: str, *,
                       action: str | None = None, by: str = "operator") -> dict:
        token = _bind_automations_telemetry(name)
        try:
            return await self._answer(name, run_id, text, action=action, by=by)
        finally:
            if token is not None:
                reset_telemetry(token)

    async def _answer(self, name: str, run_id: str, text: str, *,
                        action: str | None, by: str) -> dict:
        spec = load_automation(self._ws, name)
        record = run_log.read_run(self._ws, name, run_id)
        if not record or record.get("status") != "paused":
            raise ValueError(f"run '{run_id}' of automation '{name}' is not awaiting an answer")
        ask_kind = record.get("ask_kind")

        # Re-stamp the owner, not just the status: THIS process is the one
        # executing the resume. A parked run is answered hours or days later,
        # routinely after a restart, so the recorded owner is usually the
        # long-dead process that first fired it. Leaving it there makes the
        # crash sweep read a live `running` run as an orphan and finalize it
        # `interrupted` while it is still going.
        from durin.utils.process_tree import process_identity

        run_log.update_run(self._ws, name, run_id, status="running", owner=process_identity())
        # Release now, before the resume: the old claim is stale the moment
        # the answer arrives (idempotent — a "question" pause held no claim
        # unless a receipt registered one). If _park re-asks another tagged
        # question below, it registers a fresh claim after this point, so a
        # trailing release here would wipe it and orphan the next round-trip.
        claims.release_run(self._ws, name, run_id)

        # An explicit `action` (from webui buttons) bypasses keyword parsing:
        # it synthesizes the canonical resume text so the resume always goes
        # through the SAME text-interpretation path a free-text reply would
        # (see durin.workflow.approval.parse_approval_reply /
        # durin/service/workflows.py's resume_run_id handling) — there is
        # only one algorithm that turns text into approve/reject/revise.
        resolved_action = None
        resume_text = text
        if ask_kind == "approval":
            if action is not None:
                resolved_action = action
                resume_text = {"approve": "approve", "reject": "reject"}.get(action, text)
            else:
                resolved_action = parse_approval_reply(text) or "revise"
                resume_text = text

        try:
            result = await self._exec(spec.workflow, resume_text, resume_run_id=record["workflow_run_id"])
        except Exception as exc:  # noqa: BLE001 — any failure ends the run honestly
            rec = run_log.finalize_run(self._ws, name, run_id, status="failed", detail=str(exc),
                                        workflow_run_id=record["workflow_run_id"])
            return await self._post_finish(spec, run_id, rec, None, chain_depth=0)

        if ask_kind == "approval" and resolved_action is not None:
            run_log.record_approval(self._ws, name, run_id, action=resolved_action, by=by, at_ms=_now_ms())

        return await self._handle_result(spec, run_id, result, chain_depth=0)

    async def sweep_orphans(self) -> list[str]:
        """Finalize runs whose process died, tell somebody, relaunch what never ran.

        A run killed with its process did not fail — it produced no result at
        all. Whether relaunching it is safe turns on one question: did any
        work happen? The existence of the workflow's own manifest answers it
        — the engine writes that manifest before it walks anything, so no
        manifest means the run never got going: the original cause is still
        unserved and a fresh run is the first attempt, not a retry. Existence,
        not a count of completed nodes, is the test: a node is recorded only
        when it finishes, so a run killed inside its first node shows an
        empty node list while being the most likely to have already posted
        somewhere external — reading "no nodes" as safe would relaunch
        exactly those.

        Finalizing and notifying every orphan happens in one pass, BEFORE any
        relaunch starts: a relaunch runs a full replacement workflow (a slow
        run is the common case), and awaiting it inline here would stall
        every later orphan's own finalize/notify behind it — and, since a
        `single`-concurrency automation's manifest stays "running" until
        finalized, could even make an unrelated automation's orphan look busy
        to a concurrent fire attempt for the whole time. Each relaunch is
        instead started with `asyncio.create_task` (tracked via `_spawn`)
        once every orphan in this pass is already finalized and notified.

        A disabled (paused) automation is never relaunched — `fire`
        deliberately ignores `enabled` (a manual run-now must work on a
        paused automation), so this sweep is the only place that switch gets
        honoured for a run nobody asked for; the notice says so rather than
        promising a replacement that will never come.
        """
        from durin.workflow import run_log as wf_run_log

        handled: list[str] = []
        to_relaunch: list[tuple[dict, str]] = []
        for rec in run_log.find_orphans(self._ws):
            automation_name = rec.get("automation") or ""
            run_id = rec.get("run_id") or ""
            if not automation_name or not run_id:
                continue
            try:
                spec = load_automation(self._ws, automation_name)
            except AutomationNotFound:
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
                # replacement would run an automation its owner switched off —
                # say so instead, so the recipient isn't left expecting one.
                detail = ("interrupted by a restart before the workflow started; "
                          "not relaunched — the automation is paused")
                new_run_id = None
            else:
                new_run_id = self.reserve_run_id()
                detail = ("interrupted by a restart before the workflow started; "
                          f"relaunched as {new_run_id}")

            record = run_log.finalize_run(self._ws, automation_name, run_id, status="interrupted",
                                           workflow_run_id=wf_run_id, detail=detail)
            logger.info("automations: reconciled orphaned automation '{}' run {} as interrupted — {}",
                        automation_name, run_id, detail)
            await self._post_finish(spec, run_id, record, None, chain_depth=0)
            handled.append(run_id)

            if new_run_id is not None:
                to_relaunch.append((rec, new_run_id))

        for rec, new_run_id in to_relaunch:
            logger.info("automations: relaunching automation '{}' run {} as {} — no work had started",
                        rec.get("automation") or "", rec.get("run_id") or "", new_run_id)
            self._spawn(self._relaunch_orphan(rec, new_run_id))
        return handled

    async def _relaunch_orphan(self, rec: dict, new_run_id: str) -> None:
        """Fire the replacement run for one orphan, backgrounded by sweep_orphans."""
        automation_name = rec.get("automation") or ""
        run_id = rec.get("run_id") or ""
        origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else None
        cause = rec.get("cause") if isinstance(rec.get("cause"), dict) else {}
        try:
            await self.fire(automation_name, source=cause.get("kind") or "cron",
                             task=cause.get("excerpt") or None, origin=origin, run_id=new_run_id)
        except AutomationBusy:
            # A single-concurrency automation already has a live run — the
            # cause is being served, so a second run must not be stacked on
            # it. The promised replacement still never happened, and only the
            # recipient can judge whether the live run covers this cause.
            logger.info("automations: not relaunching '{}' run {} — already busy",
                        automation_name, run_id)
            await self.report_no_outcome(
                automation_name, new_run_id, origin=origin,
                reason=(f"the automation already had an active run when this replacement for "
                        f"run {run_id} was due to start"),
            )
        except Exception as exc:  # noqa: BLE001 — one bad orphan must not go unlogged
            logger.exception("automations: relaunch of automation '{}' run {} failed",
                              automation_name, run_id)
            await self.report_no_outcome(
                automation_name, new_run_id, origin=origin,
                reason=f"this replacement for run {run_id} failed to start: {exc}",
            )

    async def report_no_outcome(self, automation: str, run_id: str, *,
                                 origin: dict | None, reason: str) -> None:
        """Retract a run id that was announced before the run existed.

        Handing out a run id (the replacement named in an interrupted notice)
        promises that an outcome follows. When the fire never happens, that
        promise breaks with nothing behind it but a log line — so the
        retraction goes to the same recipient the outcome would have reached.
        Delivered directly (bypassing the delivery-policy/route() dance a
        real terminal run goes through): there is no run_log record for a run
        that never started, so there is nothing to attach a delivery record
        to — this is a best-effort notice, not a governed outcome.
        """
        if self._on_outcome is None:
            return
        outcome = AutomationOutcome(
            automation=automation, run_id=run_id, status="interrupted",
            summary=f"Automation '{automation}' run {run_id} produced no outcome — {reason}.",
            origin=origin, workflow_run_id=None, final_route_label=None,
        )
        try:
            await self._on_outcome(outcome)
        except Exception:  # noqa: BLE001 — delivery is best-effort
            logger.exception("automations: retraction delivery for '{}' failed", automation)

    async def _run(self, spec: AutomationSpec, *, source: str, task: str | None,
                     origin: dict | None = None, run_id: str | None = None,
                     chain_depth: int = 0) -> dict:
        run_id = run_id or self._run_id()
        run_log.start_run(self._ws, spec.name, run_id,
                           cause={"kind": source, "excerpt": task or "", "trigger_index": None},
                           origin=origin)
        # Reserve the workflow's run id and persist it BEFORE launching: if
        # this process dies mid-run, the reserved id is the only handle a
        # later sweep has for asking whether the workflow ever started.
        wf_run_id = self._run_id()
        run_log.update_run(self._ws, spec.name, run_id, workflow_run_id=wf_run_id)
        emit_tool_event("automations.fired", {"automation": spec.name, "source": source, "skipped": False})

        # A channel-triggered fire's origin thread becomes work_key ONLY when
        # it is a correlate-derived claim key (matcher-minted as
        # "custom:<automation>:<capture>", the same spec.name and the SAME
        # key claims.register uses below to wake this run), bounded to a real
        # entity the operator's regex captured. A plain per-channel thread_key
        # has no such bound, so it passes no work_key and falls back to the
        # engine's fresh per-run folder, same as a cron/manual/chain fire.
        thread = origin.get("thread") if origin else None
        work_key = (thread if thread is not None and thread.startswith(f"custom:{spec.name}:")
                    else None)
        try:
            result = await self._exec(spec.workflow, task, run_id=wf_run_id, work_key=work_key,
                                       root_session_key=f"automation:{spec.name}")
        except Exception as exc:  # noqa: BLE001 — any execution failure ends the run honestly
            record = run_log.finalize_run(self._ws, spec.name, run_id, status="failed",
                                           detail=str(exc), workflow_run_id=wf_run_id)
            return await self._post_finish(spec, run_id, record, None, chain_depth=chain_depth)
        return await self._handle_result(spec, run_id, result, chain_depth=chain_depth)

    async def _handle_result(self, spec: AutomationSpec, run_id: str, result: WorkflowResult,
                               *, chain_depth: int) -> dict:
        wf_run_id = result.run_id or None
        if (result.status in ("aborted", "cancelled") and not result.rejected
                and self._is_shutting_down()):
            # A graceful shutdown (SIGTERM) cancels every in-flight workflow
            # the same way a deliberate tasks(action='stop') does — both
            # arrive here identically. They must not be treated alike: a
            # deliberate stop really is over and finalizes as usual below,
            # but a run cut short by the gateway going down has not failed,
            # only not finished yet. Finalizing it here — even as
            # 'interrupted' — would take it out of 'running', and
            # find_orphans only ever looks at 'running' manifests: that would
            # silently disable the one mechanism (the next start's orphan
            # sweep) that finalizes it with a reason, reports it, and
            # relaunches it if nothing had started. So leave the manifest
            # exactly as it is and report nothing: the run isn't over. A
            # genuine approval rejection (rejected=True) is excluded — that
            # is a deliberate human "no", never a shutdown artifact, even
            # though it also carries status="cancelled".
            return run_log.read_run(self._ws, spec.name, run_id) or {
                "run_id": run_id, "automation": spec.name, "status": "running",
            }

        status = classify(result, spec)
        if status == "paused":
            return await self._park(spec, run_id, result)
        record = run_log.finalize_run(self._ws, spec.name, run_id, status=status,
                                       workflow_run_id=wf_run_id,
                                       final_route_label=result.final_route_label)
        return await self._post_finish(spec, run_id, record, result, chain_depth=chain_depth)

    async def _park(self, spec: AutomationSpec, run_id: str, result: WorkflowResult) -> dict:
        ask_kind = result.ask_kind
        is_counterpart, ask = _parse_ask(result.final_output or "")
        if is_counterpart:
            origin = (run_log.read_run(self._ws, spec.name, run_id) or {}).get("origin")
            thread = origin.get("thread") if isinstance(origin, dict) else None
            if thread:
                record = run_log.update_run(self._ws, spec.name, run_id, status="paused",
                                             ask=ask, ask_kind=ask_kind, proposal=None)
                claims.register(self._ws, key=thread, automation=spec.name, run_id=run_id)
                await self._say_counterpart(spec, run_id, origin, ask)
                return record
            ask += _COUNTERPART_UNAVAILABLE_NOTE

        proposal = ask if ask_kind == "approval" else None
        record = run_log.update_run(self._ws, spec.name, run_id, status="paused",
                                     ask=ask, ask_kind=ask_kind, proposal=proposal)
        receipt = await self._say_help(spec, run_id, ask_kind,
                                        f"[{spec.name} · {run_id}] {ask}", proposal)
        if receipt is not None and receipt.thread_key:
            claims.register(self._ws, key=receipt.thread_key, automation=spec.name, run_id=run_id)
        return record

    async def _post_finish(self, spec: AutomationSpec, run_id: str, record: dict,
                             result: WorkflowResult | None, *, chain_depth: int) -> dict:
        status = record["status"]
        emit_tool_event("automations.run_finished", {
            "automation": spec.name, "run_id": run_id, "status": status,
            "final_route_label": record.get("final_route_label"),
        })
        achieved = status == "achieved"
        deliver = achieved or should_deliver(status, record.get("final_route_label"), spec.delivery)
        outcome = build_outcome(spec.name, record)
        dest = route(outcome, deliver=deliver, delivery=spec.delivery, help=spec.help)
        await self._deliver_outcome(spec, run_id, outcome, dest)

        disabled_now = False
        if achieved:
            # Disabling affects future triggers only: this run's own delivery
            # (above) and chain dispatch (below) still happen.
            disabled_now = self._disable(spec, reason="achieved")
        elif (status in ("failed", "completed") and spec.life is not None
                and spec.life.max_attempts is not None):
            # Gated on THIS run's own status counting toward the streak — a
            # "rejected" or "interrupted" run is streak-transparent
            # (run_log.STREAK_TRANSPARENT) and must not re-trigger the stuck
            # check on its own: consecutive_unachieved would still report
            # whatever an OLDER streak already reached, and re-firing
            # escalate_pause/notify on every subsequent transparent run would
            # spam the help destination and redundantly re-save the
            # automation without any NEW failure having occurred.
            streak = run_log.consecutive_unachieved(self._ws, spec.name)
            if streak >= spec.life.max_attempts:
                disabled_now = await self._on_stuck(spec, run_id, streak)

        # Housekeeping only, and it must stay contained from here on: a
        # failure pruning or draining must not make a caller of fire() treat
        # a run whose real outcome already went out as if it produced none.
        try:
            run_log.prune_runs(self._ws, spec.name, self._keep_runs)
            # The run that just finished held the only concurrency slot a
            # `single` automation allows; if a channel event piled up in the
            # queue while it ran, fire it now instead of waiting for the next
            # inbound message. Never when this run just disabled the
            # automation (achieved or escalate_pause) — auto-refiring
            # something that was just switched off would defeat the point.
            if spec.enabled and not disabled_now and spec.concurrency == "single":
                event = queue.pop_fresh(self._ws, spec.name, self._queue_ttl_s)
                if event is not None:
                    # source/chain_depth default to "channel"/0 for an
                    # ordinary channel-queued event (which never carries
                    # them) — only a chain-originated event queued by
                    # _chain_fire's busy handler sets them, and they must
                    # ride along so the resumed fire picks up the same chain
                    # hop count instead of silently restarting at depth 0.
                    origin = event.get("origin")
                    emit_tool_event("automations.event_matched", {
                        "automation": spec.name,
                        "source_channel": (origin or {}).get("channel", ""),
                        "action": "drained",
                    })
                    self._spawn(self._drain_fire(spec.name, task=event.get("content"),
                                                  origin=origin,
                                                  source=event.get("source") or "channel",
                                                  chain_depth=event.get("chain_depth") or 0))
        except Exception:  # noqa: BLE001 — contained; see comment above
            logger.exception(
                "automations: post-outcome housekeeping (prune/queue-drain) for '{}' run {} failed",
                spec.name, run_id,
            )

        await self._dispatch_chains(spec.name, status, result, outcome, chain_depth)
        return record

    def _disable(self, spec: AutomationSpec, *, reason: str) -> bool:
        try:
            save_automation(self._ws, dataclasses.replace(spec, enabled=False),
                             actor="system", reason=reason)
            return True
        except Exception:  # noqa: BLE001 — disabling is best-effort; the run itself already finished
            logger.exception("automations: failed to disable '{}' (reason={})", spec.name, reason)
            return False

    async def _on_stuck(self, spec: AutomationSpec, run_id: str, streak: int) -> bool:
        mode = spec.life.on_stuck
        if mode == "keep":
            return False
        emit_tool_event("automations.escalated", {"automation": spec.name, "run_id": run_id,
                                                   "consecutive_unachieved": streak})
        text = f"automation '{spec.name}' has not reached its goal in {streak} consecutive attempts"
        await self._say_help(spec, run_id, "escalation", text, None)
        if mode == "escalate_pause":
            return self._disable(spec, reason="stuck")
        return False

    async def _deliver_outcome(self, spec: AutomationSpec, run_id: str,
                                 outcome: AutomationOutcome, dest) -> None:
        at_ms = _now_ms()
        if dest is None:
            channel, to, result = spec.delivery.channel or "", spec.delivery.to or "", "silenced"
            run_log.record_delivery(self._ws, spec.name, run_id, channel=channel, to=to,
                                     result=result, at_ms=at_ms)
            emit_tool_event("automations.delivered", {"automation": spec.name, "run_id": run_id,
                                                       "channel": channel, "result": result})
            return
        if dest.kind == "session":
            # record_delivery's channel/to are plain (non-Optional) strings
            # for the run log, so a session destination — which has no
            # channel/to of its own, only a session_key inside `origin` —
            # gets a synthetic "session"/<session_key> pair there. The
            # outcome handed to on_outcome is NOT given this synthetic pair:
            # it keeps channel=None/to=None (matching Destination's own
            # session shape) since the wiring layer routes a session
            # destination from `outcome.origin["session_key"]`, same as
            # `dest.origin` here.
            channel, to = "session", (dest.origin or {}).get("session_key") or ""
        else:
            channel, to = dest.channel or "", dest.to or ""
        # The wiring layer must receive the routed destination directly
        # rather than recompute it — see AutomationOutcome's own field
        # comments for why re-deriving it independently is unsafe.
        routed_outcome = dataclasses.replace(outcome, kind=dest.kind, channel=dest.channel, to=dest.to)
        result = "delivered"
        if self._on_outcome is not None:
            try:
                await self._on_outcome(routed_outcome)
            except Exception:  # noqa: BLE001 — delivery is best-effort; still recorded as failed
                logger.exception("automations: outcome delivery for '{}' run {} failed", spec.name, run_id)
                result = "failed"
        run_log.record_delivery(self._ws, spec.name, run_id, channel=channel, to=to,
                                 result=result, at_ms=at_ms)
        emit_tool_event("automations.delivered", {"automation": spec.name, "run_id": run_id,
                                                   "channel": channel, "result": result})

    async def _dispatch_chains(self, name: str, status: str, result: WorkflowResult | None,
                                 outcome: AutomationOutcome, chain_depth: int) -> None:
        targets = chain_targets(self._ws, finished=name, outcome=status)
        if not targets:
            return
        if chain_depth >= CHAIN_HOP_CAP:
            logger.warning("automations: chain dispatch from '{}' refused at depth {} (cap {})",
                            name, chain_depth, CHAIN_HOP_CAP)
            return
        chain_task = (result.final_output if result is not None and result.final_output else None) \
            or outcome.summary
        seen: set[str] = set()
        for target_spec, _trig in targets:
            if target_spec.name in seen:
                continue
            seen.add(target_spec.name)
            self._spawn(self._chain_fire(target_spec.name, chain_task, chain_depth + 1))

    async def _chain_fire(self, target_name: str, task: str, chain_depth: int) -> None:
        """Fire one chain-dispatched target, backgrounded by _dispatch_chains.

        Runs via `_spawn` (asyncio.create_task under the hood), so an
        exception raised in here never reaches the finishing run's own
        caller — it would only ever surface as asyncio's own untraceable
        "Task exception was never retrieved" warning, silently losing the
        whole downstream fire. Every path below must therefore handle its
        own failure instead of letting anything propagate.
        """
        try:
            await self.fire(target_name, source="chain", task=task, chain_depth=chain_depth)
        except AutomationBusy:
            # Only ever raised when the target is single-concurrency and
            # already has an active run — the same holding area the queue
            # drain already services for a busy channel-triggered fire.
            # Losing the chain here would drop the whole downstream fire with
            # nothing to show for it; queuing it instead means the target's
            # own next _post_finish drains it once its active run frees the
            # concurrency slot. The event carries its own source/chain_depth
            # so _drain_fire's eventual re-fire resumes the SAME chain hop
            # count instead of restarting at depth 0 — losing that would
            # silently defeat CHAIN_HOP_CAP for any chain that happens to
            # collide with a busy target along the way.
            try:
                queue.push(self._ws, target_name, {"content": task, "origin": None,
                                                    "source": "chain", "chain_depth": chain_depth})
            except Exception:  # noqa: BLE001 — the queue write itself must not go unlogged either
                logger.exception("automations: queuing the busy chained fire for '{}' failed", target_name)
                return
            logger.warning(
                "automations: chained fire for '{}' lost the fire race (now busy); "
                "queued for next available slot", target_name
            )
        except Exception:  # noqa: BLE001 — a chained fire's failure must not go unlogged
            logger.exception("automations: chained fire for '{}' raised an unhandled exception", target_name)

    async def _say_help(self, spec: AutomationSpec, run_id: str, kind: str, text: str, proposal):
        if self._help is None:
            return None
        try:
            return await self._help(spec, run_id, kind, text, proposal)
        except Exception:  # noqa: BLE001 — notification is best-effort
            logger.exception("automations: help notification for '{}' run {} failed", spec.name, run_id)
            return None

    async def _say_counterpart(self, spec: AutomationSpec, run_id: str, origin: dict, text: str) -> None:
        if self._notify_counterpart is None:
            return
        try:
            await self._notify_counterpart(spec.name, run_id, origin, text)
        except Exception:  # noqa: BLE001 — delivery is best-effort, mirrors _say_help
            logger.exception("automations: counterpart notification for '{}' run {} failed",
                              spec.name, run_id)

    async def _drain_fire(self, automation_name: str, task: str | None, origin: dict | None,
                            *, source: str = "channel", chain_depth: int = 0) -> None:
        """Fire a drained event, re-enqueueing and logging if the automation is busy.

        Called via create_task from _post_finish, so unhandled exceptions are
        logged by asyncio as task exceptions. On AutomationBusy, push the
        event BACK via queue.push and log a warning (mirrors how the matcher's
        own fire handles the race). Other exceptions are logged (run-level
        failures are already finalized by fire itself). `source`/`chain_depth`
        default to the ordinary channel-queued shape but ride through
        unchanged for a chain-originated event, both into the resumed `fire`
        call and into any re-queue below — losing either here would
        mislabel the run's cause or silently reset its chain hop count.
        """
        try:
            await self.fire(automation_name, source=source, task=task, origin=origin,
                             chain_depth=chain_depth)
        except AutomationBusy:
            try:
                queue.push(self._ws, automation_name, {"content": task, "origin": origin,
                                                        "source": source, "chain_depth": chain_depth})
            except Exception:  # noqa: BLE001 — the queue write itself must not go unlogged either
                logger.exception("automations: re-queuing the busy drained event for '{}' failed",
                                  automation_name)
                return
            logger.warning(
                "automations: drained event for '{}' lost the fire race (now busy); "
                "re-queued for next available slot", automation_name
            )
        except Exception:  # noqa: BLE001 — run-level errors already finalized by fire
            logger.exception("automations: drained event for '{}' raised an unhandled exception",
                              automation_name)
