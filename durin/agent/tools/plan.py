"""Plan system tools — bridge for auto-discovery by ToolLoader."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from durin.agent.tools.base import Tool

if TYPE_CHECKING:
    from durin.agent.tools.context import ToolContext

# Module-level hook reference set by PlanHook when it initializes
_plan_hook: Any = None


def set_plan_hook(hook: Any) -> None:
    global _plan_hook
    _plan_hook = hook


def get_plan_hook() -> Any:
    return _plan_hook


class SetExecutionModeTool(Tool):
    """LLM declares which execution tier to use for the current task."""

    _plugin_discoverable = True

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return _plan_hook is not None

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "set_execution_mode"

    @property
    def description(self) -> str:
        return (
            "Declare the execution mode for this task. "
            "Use 'direct' for simple answers/trivial edits that don't need verification. "
            "Use 'plan' for any task that edits code — start with a direct fix attempt "
            "(EXECUTE → VERIFY). If verification fails, escalates to a full "
            "INVESTIGATE → PLAN → EXECUTE → VERIFY cycle."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["direct", "plan"],
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
        from durin.plan.types import ExecutionTier
        execution_tier = ExecutionTier(tier)
        if _plan_hook:
            _plan_hook.set_tier(execution_tier, reason)
        return f"Execution mode set to: {execution_tier.value}"


class UpdatePlanTool(Tool):
    """LLM updates the execution plan."""

    _plugin_discoverable = True

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return _plan_hook is not None

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    @property
    def name(self) -> str:
        return "update_plan"

    @property
    def description(self) -> str:
        return (
            "Update the execution plan. Use 'add' to add steps, "
            "'complete' to mark done, 'fail' to mark failed. "
            "Only available in plan mode."
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
        if not _plan_hook:
            return "Plan system not active."
        from durin.plan.types import ExecutionTier
        if _plan_hook.state.tier != ExecutionTier.PLAN:
            return "update_plan only available in plan mode. Call set_execution_mode first."
        return _plan_hook.update_plan(action, item)
