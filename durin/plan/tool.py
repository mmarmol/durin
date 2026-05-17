"""Tools that the LLM calls to interact with the plan system."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from durin.agent.tools.base import Tool
from durin.plan.types import ExecutionTier, Phase, PlanItem

if TYPE_CHECKING:
    from durin.plan.hook import PlanHook


class SetExecutionModeTool(Tool):
    """LLM declares which execution tier to use for the current task."""

    _hook: PlanHook | None

    def __init__(self, hook: PlanHook | None = None) -> None:
        self._hook = hook

    @property
    def name(self) -> str:
        return "set_execution_mode"

    @property
    def description(self) -> str:
        return (
            "Declare the execution mode for this task. "
            "Use 'direct' for simple answers/trivial edits, "
            "'execute_verify' for localized changes that need test verification, "
            "'full_plan' for complex multi-step tasks that need investigation, planning, execution, and confirmation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["direct", "execute_verify", "full_plan"],
                    "description": "Execution tier for this task.",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason for choosing this tier (1 sentence).",
                },
            },
            "required": ["tier"],
        }

    async def execute(self, *, tier: str, reason: str = "") -> str:
        execution_tier = ExecutionTier(tier)
        if self._hook:
            self._hook.set_tier(execution_tier, reason)
        return f"Execution mode set to: {execution_tier.value}"


class UpdatePlanTool(Tool):
    """LLM updates the plan items (add, complete, fail)."""

    _hook: PlanHook | None

    def __init__(self, hook: PlanHook | None = None) -> None:
        self._hook = hook

    @property
    def name(self) -> str:
        return "update_plan"

    @property
    def description(self) -> str:
        return (
            "Update the execution plan. Use 'add' to add new steps, "
            "'complete' to mark a step as done, 'fail' to mark a step as failed. "
            "Only available in full_plan mode."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "complete", "fail"],
                },
                "item": {
                    "type": "string",
                    "description": "Step description (for 'add') or existing step text (for 'complete'/'fail').",
                },
            },
            "required": ["action", "item"],
        }

    async def execute(self, *, action: str, item: str) -> str:
        if not self._hook:
            return "Plan system not active."
        if self._hook.state.tier != ExecutionTier.FULL_PLAN:
            return "update_plan only available in full_plan mode. Call set_execution_mode first."
        return self._hook.update_plan(action, item)
