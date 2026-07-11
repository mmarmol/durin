"""Loops tool — conversational management of loop definitions and runs.

Exposes the same operations as the webui's loops surface (``durin.service.loops``)
to the agent: list/inspect loops, fire a run, answer a run awaiting operator
input, toggle enabled/paused, and create a new loop from a JSON definition —
all through ``durin.loops.store`` + ``durin.loops.run_log`` + the live
``LoopsRuntime``, so a loop the agent creates in chat goes through the exact
same validation and cron-sync path as one created via the webui. Only
available when the surface wires a ``LoopsRuntime`` onto ``ToolContext``.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.loops import run_log
from durin.loops.cron_sync import sync_loop_jobs
from durin.loops.runtime import LoopBusy
from durin.loops.spec import LoopError, LoopNotFound, parse_loop
from durin.loops.store import list_loops, load_loop, save_loop

_PARAMETERS = tool_parameters_schema(
    action=StringSchema(
        "Action to perform.",
        enum=["list", "status", "fire", "answer", "enable", "pause", "create"],
    ),
    name=StringSchema(
        "Loop name. REQUIRED for status/fire/answer/enable/pause. For "
        "action='create', if given it overrides the 'name' field inside "
        "'definition'."
    ),
    task=StringSchema(
        "Optional task text for action='fire', overriding the loop's default "
        "goal for this one run."
    ),
    answer=StringSchema("REQUIRED for action='answer': the reply to a run awaiting operator input."),
    run_id=StringSchema("REQUIRED for action='answer': the run id (from a previous fire/status)."),
    definition=StringSchema(
        "REQUIRED for action='create': the full loop definition as a JSON string "
        "— {name, workflow, goal: {intent, checks?}, triggers?, concurrency?, "
        "stuck_after?, operator_channel?, operator_to?}."
    ),
    required=["action"],
    description=(
        "Manage conversational loops. list/status inspect definitions and runs; "
        "fire manually triggers a run; answer replies to a run awaiting operator "
        "input; enable/pause toggle a loop's triggers; create defines a new loop "
        "from a JSON definition (same validation as the webui). Per-action "
        "requirements are enforced at runtime (see field descriptions)."
    ),
)


@tool_parameters(_PARAMETERS)
class LoopsTool(Tool):
    """Tool for inspecting and driving loop definitions/runs from chat."""

    _scopes = {"core"}

    def __init__(self, workspace: str, runtime: Any, cron_service: Any = None) -> None:
        self._ws = workspace
        self._runtime = runtime
        self._cron = cron_service

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.loops_runtime is not None

    @classmethod
    def create(cls, ctx: Any) -> "LoopsTool":
        return cls(workspace=ctx.workspace, runtime=ctx.loops_runtime, cron_service=ctx.cron_service)

    @property
    def name(self) -> str:
        return "loops"

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
            return await self._answer(name, run_id, answer)
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
        specs = list_loops(self._ws)
        if not specs:
            return "No loops defined."
        lines = []
        for spec in specs:
            active = run_log.active_runs(self._ws, spec.name)
            needs_op = sum(1 for r in active if r.get("status") == "needs_operator")
            state = "enabled" if spec.enabled else "paused"
            lines.append(
                f"- {spec.name} ({state}, workflow: {spec.workflow}, "
                f"active_runs: {len(active)}, needs_operator: {needs_op})"
            )
        return "Loops:\n" + "\n".join(lines)

    def _status(self, name: str) -> str:
        try:
            spec = load_loop(self._ws, name)
        except LoopNotFound as exc:
            return f"Error: {exc}"
        active = run_log.active_runs(self._ws, name)
        needs_op = sum(1 for r in active if r.get("status") == "needs_operator")
        recent = run_log.list_runs(self._ws, name, limit=5)
        state = "enabled" if spec.enabled else "paused"
        lines = [
            f"Loop '{spec.name}' ({state})",
            f"  Workflow: {spec.workflow}",
            f"  Goal: {spec.goal_intent}",
            f"  Concurrency: {spec.concurrency}, stuck_after: {spec.stuck_after}",
            f"  Active runs: {len(active)} ({needs_op} needs_operator)",
        ]
        if recent:
            lines.append("  Recent runs:")
            for r in recent:
                lines.append(f"    - {r.get('run_id')}: {r.get('status')} (source: {r.get('source')})")
        else:
            lines.append("  No runs yet.")
        return "\n".join(lines)

    async def _fire(self, name: str, task: str | None) -> str:
        try:
            record = await self._runtime.fire(name, source="chat", task=task or None)
        except LoopBusy as exc:
            return f"Loop '{name}' is busy: {exc}"
        except LoopNotFound as exc:
            return f"Error: {exc}"
        return self._format_run(name, record)

    async def _answer(self, name: str, run_id: str, answer: str) -> str:
        try:
            record = await self._runtime.answer(name, run_id, answer)
        except LoopNotFound as exc:
            return f"Error: {exc}"
        except ValueError as exc:
            return f"Error: {exc}"
        return self._format_run(name, record)

    @staticmethod
    def _format_run(name: str, record: dict) -> str:
        status = record.get("status")
        line = f"Loop '{name}' run {record.get('run_id')}: {status}"
        if status == "needs_operator" and record.get("ask"):
            line += f"\n  Asking: {record['ask']}"
        if status in ("done", "no_goal"):
            line += f" (goal_reached: {record.get('goal_reached')})"
        if status == "escalated":
            line += "\n  Escalated: repeated failures to reach the goal."
        return line

    def _set_enabled(self, name: str, enable: bool) -> str:
        try:
            spec = load_loop(self._ws, name)
        except LoopNotFound as exc:
            return f"Error: {exc}"
        new_spec = replace(spec, enabled=enable)
        save_loop(self._ws, new_spec)
        state = "enabled" if enable else "paused"
        if self._cron is None:
            return f"Loop '{name}' is now {state} (cron sync skipped: no cron service on this surface)."
        sync_loop_jobs(self._cron, new_spec)
        return f"Loop '{name}' is now {state}."

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
            spec = parse_loop(data)
        except LoopError as exc:
            return f"Error: invalid loop definition: {exc}"
        save_loop(self._ws, spec)
        if self._cron is not None:
            sync_loop_jobs(self._cron, spec)
        state = "enabled" if spec.enabled else "paused"
        return f"Created loop '{spec.name}' (workflow: {spec.workflow}, {state})."
