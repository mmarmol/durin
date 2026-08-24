"""Automations tool — conversational management of automation definitions and runs.

Exposes the same operations as the webui's automations surface
(``durin.service.automations``) to the agent: list/inspect automations, fire a
run, answer a run paused for an operator or counterpart reply, toggle
enabled/paused, and create a new automation from a JSON definition — all
through ``durin.automations.store`` + ``durin.automations.run_log`` + the live
``AutomationsRuntime``, so an automation the agent creates in chat goes
through the exact same validation and cron-sync path as one created via the
webui. Only available when the surface wires an ``AutomationsRuntime`` onto
``ToolContext``.

Single-case doctrine: an automation is the unit of standing work, and one
carrying a ``life`` condition is single-case, not a reusable template — see
the tool description below and the ``automations`` skill.
"""
from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

from loguru import logger

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.automations import queue, run_log
from durin.automations.cron_sync import sync_automation_jobs
from durin.automations.runtime import AutomationBusy
from durin.automations.spec import AutomationError, AutomationNotFound, parse_automation
from durin.automations.store import list_automations, load_automation, save_automation

_PARAMETERS = tool_parameters_schema(
    action=StringSchema(
        "Action to perform.",
        enum=["list", "status", "fire", "answer", "enable", "pause", "create"],
    ),
    name=StringSchema(
        "Automation name. REQUIRED for status/fire/answer/enable/pause. For "
        "action='create', if given it overrides the 'name' field inside "
        "'definition'."
    ),
    task=StringSchema(
        "Optional task text for action='fire', overriding a schedule trigger's "
        "default task for this one run."
    ),
    answer=StringSchema("REQUIRED for action='answer': the reply to a run paused for an answer."),
    run_id=StringSchema("REQUIRED for action='answer': the run id (from a previous fire/status)."),
    resolution=StringSchema(
        "Optional for action='answer', only when the paused run is an APPROVAL: bypasses "
        "keyword parsing of 'answer' and resolves it directly.",
        enum=["approve", "revise", "reject"],
    ),
    definition=StringSchema(
        "REQUIRED for action='create': the full automation definition as a JSON string "
        "— {name, workflow, triggers?, delivery?, help?, life?, concurrency?}."
    ),
    required=["action"],
    description=(
        "Manage automations — durin's standing triggers (a schedule, a channel message, "
        "a webhook, or another automation's outcome) that fire a workflow and deliver its "
        "result. This tool is the single source of truth for automations; their schedule "
        "triggers also appear in `cron list` as read-only `automation:*` system jobs — "
        "never manage them there. list/status inspect definitions and runs; fire manually starts a run; "
        "answer replies to a run paused for an operator or counterpart reply; "
        "enable/pause toggle a definition's triggers; create defines a new automation "
        "from a JSON definition (same validation as the webui) — sending 'create' again "
        "with an existing name replaces that definition wholesale. SINGLE-CASE DOCTRINE: "
        "an automation carrying a 'life' condition (a verifiable achieved-when check) is "
        "single-case, not a reusable template — \"chase invoice X\" means creating ONE "
        "dedicated automation for X (typically via create, from this chat), which "
        "disables itself once its life condition is achieved. Do not build one generic "
        "automation and expect it to track many unrelated cases at once; each case gets "
        "its own. Per-action requirements are enforced at runtime (see field "
        "descriptions)."
    ),
)


@tool_parameters(_PARAMETERS)
class AutomationsTool(Tool):
    """Tool for inspecting and driving automation definitions/runs from chat."""

    _scopes = {"core"}

    def __init__(self, workspace: str, runtime: Any, cron_service: Any = None) -> None:
        self._ws = workspace
        self._runtime = runtime
        self._cron = cron_service
        # Who asked. A run fired from a conversation reports back into that
        # conversation; without this the outcome falls through to the
        # automation's declared destination, which is often unset.
        self._origin: ContextVar[dict | None] = ContextVar("automations_origin", default=None)
        # Strong references: an un-awaited task is collectable mid-flight.
        self._fires: set = set()

    def set_context(self, ctx: Any) -> None:
        if not ctx.session_key:
            self._origin.set(None)
            return
        self._origin.set({
            "kind": "session",
            "session_key": ctx.session_key,
            "channel": ctx.channel,
            "chat_id": ctx.chat_id,
        })

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.automations_runtime is not None

    @classmethod
    def create(cls, ctx: Any) -> "AutomationsTool":
        return cls(workspace=ctx.workspace, runtime=ctx.automations_runtime, cron_service=ctx.cron_service)

    @property
    def name(self) -> str:
        return "automations"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        action = params.get("action")
        if action in ("status", "fire", "answer", "enable", "pause") and not str(params.get("name") or "").strip():
            errors.append(f"name is required when action='{action}'")
        if action == "answer":
            if not str(params.get("run_id") or "").strip():
                errors.append("run_id is required when action='answer'")
            if not str(params.get("answer") or "").strip():
                errors.append("answer is required when action='answer'")
        if action == "create" and not str(params.get("definition") or "").strip():
            errors.append("definition is required when action='create'")
        return errors

    async def execute(
        self,
        action: str,
        name: str | None = None,
        task: str | None = None,
        answer: str | None = None,
        run_id: str | None = None,
        resolution: str | None = None,
        definition: str | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "list":
            return self._list()
        if action == "status":
            if not name:
                return "Error: status requires 'name'"
            return self._status(name)
        if action == "fire":
            if not name:
                return "Error: fire requires 'name'"
            return await self._fire(name, task)
        if action == "answer":
            if not name or not run_id or not answer:
                return "Error: answer requires 'name', 'run_id', and 'answer'"
            return await self._answer(name, run_id, answer, resolution)
        if action == "enable":
            if not name:
                return "Error: enable requires 'name'"
            return self._set_enabled(name, True)
        if action == "pause":
            if not name:
                return "Error: pause requires 'name'"
            return self._set_enabled(name, False)
        if action == "create":
            if not definition:
                return "Error: create requires 'definition' (a JSON string)"
            return self._create(name, definition)
        return f"Unknown action: {action}"

    def _list(self) -> str:
        specs = list_automations(self._ws)
        if not specs:
            return "No automations defined."
        lines = []
        for spec in specs:
            active = run_log.active_runs(self._ws, spec.name)
            awaiting = sum(1 for r in active if r.get("status") == "paused")
            state = "enabled" if spec.enabled else "paused"
            lines.append(
                f"- {spec.name} ({state}, workflow: {spec.workflow}, "
                f"active_runs: {len(active)}, awaiting_answer: {awaiting})"
            )
        return "Automations:\n" + "\n".join(lines)

    def _status(self, name: str) -> str:
        try:
            spec = load_automation(self._ws, name)
        except AutomationNotFound as exc:
            return f"Error: {exc}"
        active = run_log.active_runs(self._ws, name)
        awaiting = sum(1 for r in active if r.get("status") == "paused")
        pending = queue.pending(self._ws, name)
        recent = run_log.list_runs(self._ws, name, limit=5)
        state = "enabled" if spec.enabled else "paused"
        lines = [
            f"Automation '{spec.name}' ({state})",
            f"  Workflow: {spec.workflow}",
            f"  Triggers: {len(spec.triggers)}",
            f"  Concurrency: {spec.concurrency}",
        ]
        if spec.life is not None:
            attempts = run_log.consecutive_unachieved(self._ws, name)
            attempts_str = (
                f"{attempts}/{spec.life.max_attempts}" if spec.life.max_attempts else str(attempts)
            )
            lines.append(
                f"  Life: {spec.life.intent} (achieved_when: {spec.life.achieved_when}, "
                f"attempts: {attempts_str}, on_stuck: {spec.life.on_stuck})"
            )
        lines.append(f"  Active runs: {len(active)} ({awaiting} awaiting an answer)")
        lines.append(f"  Queued events: {pending}")
        if recent:
            lines.append("  Recent runs:")
            for r in recent:
                cause = (r.get("cause") or {}).get("kind")
                lines.append(f"    - {r.get('run_id')}: {r.get('status')} (cause: {cause})")
        else:
            lines.append("  No runs yet.")
        return "\n".join(lines)

    async def _fire(self, name: str, task: str | None) -> str:
        """Start a run and return; the outcome arrives as a follow-up.

        An automation run is read, not driven — holding the agent's turn open
        for the length of a multi-stage workflow makes the turn hostage to a
        run the agent cannot influence, and loses the run entirely if this
        process dies. Busy and not-found are decided before anything is
        launched, so they still answer inline.
        """
        try:
            spec = load_automation(self._ws, name)
        except AutomationNotFound as exc:
            return f"Error: {exc}"
        if spec.concurrency == "single" and run_log.active_runs(self._ws, name):
            return f"Automation '{name}' is busy: an active run already exists"

        run_id = self._runtime.reserve_run_id()
        origin = self._origin.get()
        task_handle = asyncio.create_task(self._background_fire(name, task, origin, run_id))
        self._fires.add(task_handle)
        task_handle.add_done_callback(self._fires.discard)
        if origin is None:
            return (
                f"Automation '{name}' started (run id: {run_id}). This surface has no "
                "conversation origin wired, so its outcome cannot be delivered back here "
                f"as a follow-up — use automations(action='status', name='{name}') to "
                "check on it."
            )
        return (
            f"Automation '{name}' started (run id: {run_id}). Its outcome will be "
            "delivered to you automatically as a follow-up message when it finishes — "
            "do NOT poll for it: tell the user it is running and end your turn. Use "
            f"automations(action='status', name='{name}') only if the user asks for an "
            "update."
        )

    async def _background_fire(
        self, name: str, task: str | None, origin: dict | None, run_id: str,
    ) -> None:
        """Run the fire task in the background and never let its outcome vanish.

        `asyncio.create_task` in `_fire` schedules this but nothing awaits it,
        so any exception it raises would otherwise only surface as an
        unretrieved-task-exception warning at GC time — the agent has already
        been told the run started and no one is watching for the failure.
        Mirrors the automations runtime's own dispatch paths: a distinct
        `AutomationBusy` branch for the automation going busy between this
        call's pre-check and the task actually running (e.g. two fires issued
        in the same batch — the pre-check can't see a sibling task's run
        until it writes its own run_log entry), and a catch-all for
        everything else (AutomationNotFound if the automation is deleted
        mid-flight, a run_log write error, ...).

        Both branches also retract the run id through the runtime's outcome
        path. The agent was already told this run's outcome arrives as a
        follow-up and instructed not to poll, so a failure the log alone
        records leaves it waiting on a message that will never come.
        """
        try:
            await self._runtime.fire(name, source="chat", task=task or None, origin=origin, run_id=run_id)
        except AutomationBusy:
            logger.warning(
                "automations: chat fire for automation '{}' lost the race (now busy); run {} never started",
                name, run_id,
            )
            await self._report_no_outcome(
                name, run_id, origin,
                "the automation went busy with another run before this one could start",
            )
        except Exception as exc:
            logger.exception(
                "automations: backgrounded chat fire for automation '{}' (run {}) failed", name, run_id,
            )
            await self._report_no_outcome(name, run_id, origin, f"the fire failed: {exc}")

    async def _report_no_outcome(
        self, name: str, run_id: str, origin: dict | None, reason: str,
    ) -> None:
        """Tell whoever was promised this run's outcome that none is coming.

        Best-effort: the runtime's own outcome callback is optional (a
        surface can wire an `AutomationsRuntime` with no `on_outcome` at all —
        `report_no_outcome` itself handles that), but this call must never
        turn one background failure into a second, unhandled one.
        """
        try:
            await self._runtime.report_no_outcome(name, run_id, origin=origin, reason=reason)
        except Exception:
            logger.exception(
                "automations: could not report the failed fire of automation '{}' run {}", name, run_id,
            )

    async def _answer(self, name: str, run_id: str, answer: str, resolution: str | None) -> str:
        """Resume a paused run and return; the outcome arrives as a follow-up.

        Mirrors `_fire`: the resume is a full workflow run and can take as
        long as the original fire would have, so the tool call must not
        hold the agent's turn open for it. `AutomationsRuntime.answer_nowait`
        already backgrounds the actual resume itself (see its own
        docstring) — no extra asyncio.create_task wrapping needed here,
        just await its quick synchronous prologue and report that it
        resumed.
        """
        try:
            record = await self._runtime.answer_nowait(name, run_id, answer, action=resolution, by="agent")
        except AutomationNotFound as exc:
            return f"Error: {exc}"
        except ValueError as exc:
            return f"Error: {exc}"
        return (
            f"Automation '{name}' run {record.get('run_id')} resumed in the background — "
            "do NOT poll for it; its outcome arrives through the automation's normal "
            "delivery/help routing once the resume finishes. Use "
            f"automations(action='status', name='{name}') only if the user asks for an "
            "update."
        )

    def _set_enabled(self, name: str, enable: bool) -> str:
        try:
            spec = load_automation(self._ws, name)
        except AutomationNotFound as exc:
            return f"Error: {exc}"
        new_spec = replace(spec, enabled=enable)
        state = "enabled" if enable else "paused"
        save_automation(self._ws, new_spec, actor="agent", reason=f"automation {state}")
        if self._cron is None:
            return f"Automation '{name}' is now {state} (cron sync skipped: no cron service on this surface)."
        sync_automation_jobs(self._cron, new_spec)
        return f"Automation '{name}' is now {state}."

    def _create(self, name: str | None, definition: str) -> str:
        try:
            data = json.loads(definition)
        except json.JSONDecodeError as exc:
            return f"Error: definition is not valid JSON: {exc}"
        if not isinstance(data, dict):
            return "Error: definition must be a JSON object"
        if name:
            data = {**data, "name": name}
        try:
            spec = parse_automation(data)
            # save_automation itself re-validates the chain-trigger graph
            # (durin.automations.chains.validate_chain_edges) and raises the
            # same AutomationError on a cycle, so one except below covers both.
            save_automation(self._ws, spec, actor="agent", reason="created from chat")
        except AutomationError as exc:
            return f"Error: invalid automation definition: {exc}"
        if self._cron is not None:
            sync_automation_jobs(self._cron, spec)
        state = "enabled" if spec.enabled else "paused"
        return f"Created automation '{spec.name}' (workflow: {spec.workflow}, {state})."
