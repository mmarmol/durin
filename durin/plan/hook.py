"""PlanHook — enforces execution tiers and the investigate→plan→execute→confirm cycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from durin.agent.hook import AgentHook, AgentHookContext
from durin.plan.store import PlanStore
from durin.plan.types import ExecutionTier, Phase, PlanItem, PlanState


_TIER_INSTRUCTIONS = """\
[Execution Modes]
Before starting work, declare your execution mode by calling set_execution_mode:

- direct: Simple answers, trivial edits, explanations. No verification needed.
- execute_verify: Localized bug fix or single change. Edit, then verify with tests.
- full_plan: Complex task, uncertainty, multi-step. You MUST follow the cycle:
  INVESTIGATE → PLAN → EXECUTE → CONFIRM (repeat if confirm fails).
  Use update_plan to track steps. You cannot declare done with pending steps.

Choose the tier that matches the task complexity."""


_VERIFY_REMINDER = (
    "[Reminder] You edited code in execute_verify mode. "
    "Run relevant tests to verify your change before completing."
)


_PHASE_PROMPTS: dict[Phase, str] = {
    Phase.INVESTIGATE: (
        "[Plan: INVESTIGATE] Read files, understand context. Do NOT edit yet. "
        "Once you understand the problem, call update_plan to add steps."
    ),
    Phase.PLAN: (
        "[Plan: PLAN] Define or update your plan steps via update_plan(action='add', item='...'). "
        "When your plan is ready, proceed to EXECUTE."
    ),
    Phase.EXECUTE: (
        "[Plan: EXECUTE] Implement the current step. Edit files as needed. "
        "When done, move to CONFIRM by running tests."
    ),
    Phase.CONFIRM: (
        "[Plan: CONFIRM] Run tests or validation to verify your changes. "
        "If tests pass, mark the step complete. If they fail, investigate why."
    ),
}


class PlanHook(AgentHook):
    """Manages execution tiers. Only enforces cycle for Tier 3."""

    __slots__ = ("_state", "_store", "_edit_detected", "_exec_detected", "_tier_set", "_pending_bias_events")

    def __init__(
        self,
        workspace: Path | None = None,
        session_key: str = "default",
    ) -> None:
        super().__init__()
        self._state = PlanState(goal="")
        self._store: PlanStore | None = None
        if workspace:
            self._store = PlanStore(workspace, session_key)
            existing = self._store.load_state()
            if existing:
                self._state = existing
        self._edit_detected = False
        self._exec_detected = False
        self._tier_set = False
        self._pending_bias_events: list[str] = []

        # Register hook reference for tool discovery
        from durin.agent.tools.plan import set_plan_hook
        set_plan_hook(self)

    @property
    def state(self) -> PlanState:
        return self._state

    @property
    def tier_is_set(self) -> bool:
        return self._tier_set

    def set_tier(self, tier: ExecutionTier, reason: str = "") -> None:
        self._state.tier = tier
        self._tier_set = True
        if tier == ExecutionTier.FULL_PLAN:
            self._state.current_phase = Phase.INVESTIGATE
            self._state.cycle_count = 1
        if self._store:
            self._store.append_event("tier_set", tier=tier.value, reason=reason)
            self._store.save_state(self._state)
        logger.info("PlanHook: tier set to {} ({})", tier.value, reason)

    def get_plan_bias_events(self) -> list[str]:
        """Layer 3: return stimulus events based on plan complexity."""
        if self._state.tier != ExecutionTier.FULL_PLAN:
            return []
        events = []
        if len(self._state.items) > 3:
            events.append("plan_complex")
        if self._state.cycle_count >= 2:
            events.append("cycle_restart")
        return events

    def update_plan(self, action: str, item: str) -> str:
        state = self._state
        if state.tier != ExecutionTier.FULL_PLAN:
            return "update_plan only available in full_plan mode. Call set_execution_mode first."

        if action == "add":
            plan_item = PlanItem(
                description=item,
                status="pending",
                added_at_cycle=state.cycle_count,
            )
            was_under_threshold = len(state.items) <= 3
            state.items.append(plan_item)
            if self._store:
                self._store.append_event(
                    "plan_item_added", item=item, cycle=state.cycle_count
                )
            # Emit complexity bias when plan crosses >3 items
            if was_under_threshold and len(state.items) > 3:
                self._pending_bias_events.append("plan_complex")
            # Transition from INVESTIGATE to PLAN when items are added
            if state.current_phase == Phase.INVESTIGATE:
                self._transition_phase(Phase.PLAN)
            self._save()
            return f"Added step: {item}"

        if action == "complete":
            for i in state.items:
                if i.description == item and i.status in ("pending", "in_progress"):
                    i.status = "done"
                    i.completed_at_cycle = state.cycle_count
                    if self._store:
                        self._store.append_event(
                            "plan_item_completed", item=item, cycle=state.cycle_count
                        )
                    self._save()
                    return f"Completed: {item}"
            return f"Step not found or already done: {item}"

        if action == "fail":
            for i in state.items:
                if i.description == item and i.status in ("pending", "in_progress"):
                    i.status = "failed"
                    if self._store:
                        self._store.append_event(
                            "plan_item_failed", item=item, cycle=state.cycle_count
                        )
                    self._save()
                    return f"Failed: {item}. Investigate and re-plan."
            return f"Step not found: {item}"

        return f"Unknown action: {action}"

    async def before_iteration(self, context: AgentHookContext) -> None:
        # If tier not set yet, inject instructions until the LLM declares one
        if not self._tier_set:
            self._inject_system(context, _TIER_INSTRUCTIONS)
            self._edit_detected = False
            self._exec_detected = False
            return

        tier = self._state.tier

        if tier == ExecutionTier.DIRECT:
            self._edit_detected = False
            self._exec_detected = False
            return

        if tier == ExecutionTier.EXECUTE_VERIFY:
            if self._edit_detected:
                self._inject_system(context, _VERIFY_REMINDER)
            self._edit_detected = False
            self._exec_detected = False
            return

        # FULL_PLAN: inject phase prompt + plan state
        if tier == ExecutionTier.FULL_PLAN:
            phase = self._state.current_phase or Phase.INVESTIGATE
            prompt = self._build_plan_prompt(phase)
            self._inject_system(context, prompt)
        self._edit_detected = False
        self._exec_detected = False

    async def after_iteration(self, context: AgentHookContext) -> None:
        # Detect tool usage for phase inference
        for call in context.tool_calls:
            if call.name in ("edit_file", "write_file"):
                self._edit_detected = True
            if call.name == "exec":
                self._exec_detected = True

        # Flush pending bias events to posture layer
        if self._pending_bias_events:
            context.external_stimulus_events.extend(self._pending_bias_events)
            self._pending_bias_events.clear()

        tier = self._state.tier

        if tier == ExecutionTier.FULL_PLAN:
            self._infer_phase_transition(context)
            self._save()

    def _infer_phase_transition(self, context: AgentHookContext) -> None:
        phase = self._state.current_phase
        if phase is None:
            return

        # INVESTIGATE → PLAN: when update_plan is called (handled in update_plan)
        # PLAN → EXECUTE: when edit tools are used
        if phase == Phase.PLAN and self._edit_detected:
            self._transition_phase(Phase.EXECUTE)

        # EXECUTE → CONFIRM: when exec is called after edits
        if phase == Phase.EXECUTE and self._exec_detected and self._edit_detected:
            self._transition_phase(Phase.CONFIRM)

        # CONFIRM → result handling
        if phase == Phase.CONFIRM and self._exec_detected:
            if context.error:
                self._on_confirm_fail(context)
            else:
                self._on_confirm_pass(context)

    def _on_confirm_pass(self, context: AgentHookContext) -> None:
        if self._store:
            self._store.append_event(
                "confirm_result", outcome="pass", cycle=self._state.cycle_count
            )
        # Mark current in_progress items as done implicitly
        for item in self._state.items:
            if item.status == "in_progress":
                item.status = "done"
                item.completed_at_cycle = self._state.cycle_count
        # Emit stimulus for posture layer 2
        context.external_stimulus_events.append("confirm_pass")

    def _on_confirm_fail(self, context: AgentHookContext) -> None:
        self._state.cycle_count += 1
        self._state.current_phase = Phase.INVESTIGATE
        if self._store:
            self._store.append_event(
                "confirm_result", outcome="fail", cycle=self._state.cycle_count
            )
        # Emit stimuli for posture layer 2
        context.external_stimulus_events.append("confirm_fail")
        context.external_stimulus_events.append("cycle_restart")
        logger.info(
            "PlanHook: CONFIRM failed, starting cycle {}",
            self._state.cycle_count,
        )

    def _transition_phase(self, new_phase: Phase) -> None:
        old = self._state.current_phase
        self._state.current_phase = new_phase
        if self._store:
            self._store.append_event(
                "phase_transition",
                from_phase=old.value if old else "none",
                to_phase=new_phase.value,
                cycle=self._state.cycle_count,
            )
        logger.debug("PlanHook: {} → {}", old, new_phase)

    def _build_plan_prompt(self, phase: Phase) -> str:
        parts = [f"[Plan System] Cycle {self._state.cycle_count} | Phase: {phase.value.upper()}"]

        if self._state.items:
            parts.append("Current plan:")
            for i, item in enumerate(self._state.items, 1):
                status_icon = {"pending": " ", "in_progress": "→", "done": "✓", "failed": "✗"}
                parts.append(f"  {i}. [{status_icon[item.status]}] {item.description}")

        parts.append("")
        parts.append(_PHASE_PROMPTS[phase])
        return "\n".join(parts)

    def _inject_system(self, context: AgentHookContext, content: str) -> None:
        msg = {"role": "system", "content": content}
        # Insert before the last user message for maximum visibility
        last_user_idx = None
        for i in range(len(context.messages) - 1, -1, -1):
            if context.messages[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx is not None:
            context.messages.insert(last_user_idx, msg)
        else:
            context.messages.append(msg)
        context.injected_messages_count += 1

    def _save(self) -> None:
        if self._store:
            self._store.save_state(self._state)
