"""Tests for PlanHook — tier enforcement and cycle management."""

import pytest

from durin.agent.hook import AgentHookContext
from durin.plan.hook import PlanHook
from durin.plan.types import ExecutionTier, Phase, PlanItem
from durin.providers.base import ToolCallRequest


def _make_context(
    iteration: int = 0,
    tool_calls: list | None = None,
    tool_results: list | None = None,
    error: str | None = None,
) -> AgentHookContext:
    return AgentHookContext(
        iteration=iteration,
        messages=[{"role": "user", "content": "fix the bug"}],
        tool_calls=tool_calls or [],
        tool_results=tool_results or [],
        error=error,
    )


def _tool_call(name: str) -> ToolCallRequest:
    return ToolCallRequest(id="t1", name=name, arguments={})


class TestTierSelection:
    def test_initial_state_is_direct(self):
        hook = PlanHook()
        assert hook.state.tier == ExecutionTier.DIRECT
        assert hook.tier_is_set is False

    def test_set_tier_direct(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.DIRECT, "simple question")
        assert hook.state.tier == ExecutionTier.DIRECT
        assert hook.tier_is_set is True

    def test_set_tier_full_plan_initializes_cycle(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.FULL_PLAN, "complex refactoring")
        assert hook.state.tier == ExecutionTier.FULL_PLAN
        assert hook.state.current_phase == Phase.INVESTIGATE
        assert hook.state.cycle_count == 1


class TestDirectTier:
    @pytest.mark.asyncio
    async def test_no_injection_after_tier_set(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.DIRECT)
        ctx = _make_context(iteration=1)
        await hook.before_iteration(ctx)
        assert ctx.injected_messages_count == 0


class TestExecuteVerifyTier:
    @pytest.mark.asyncio
    async def test_reminder_after_edit(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.EXECUTE_VERIFY)

        # Simulate edit detected
        ctx = _make_context(iteration=1, tool_calls=[_tool_call("edit_file")])
        await hook.after_iteration(ctx)

        # Next iteration should get reminder
        hook._edit_detected = True  # Carried from after_iteration
        ctx2 = _make_context(iteration=2)
        await hook.before_iteration(ctx2)
        assert ctx2.injected_messages_count == 1
        system_msgs = [m for m in ctx2.messages if m["role"] == "system"]
        assert any("verify" in m["content"].lower() for m in system_msgs)


class TestFullPlanTier:
    @pytest.mark.asyncio
    async def test_injects_phase_prompt(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.FULL_PLAN, "multi-step fix")
        ctx = _make_context(iteration=1)
        await hook.before_iteration(ctx)
        assert ctx.injected_messages_count == 1
        system_msgs = [m for m in ctx.messages if m["role"] == "system"]
        assert any("INVESTIGATE" in m["content"] for m in system_msgs)

    def test_update_plan_add(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.FULL_PLAN)
        result = hook.update_plan("add", "Read fitsrec.py")
        assert "Added" in result
        assert len(hook.state.items) == 1
        assert hook.state.items[0].description == "Read fitsrec.py"

    def test_update_plan_complete(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.FULL_PLAN)
        hook.update_plan("add", "Read file")
        result = hook.update_plan("complete", "Read file")
        assert "Completed" in result
        assert hook.state.items[0].status == "done"

    def test_update_plan_fail(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.FULL_PLAN)
        hook.update_plan("add", "Apply fix")
        result = hook.update_plan("fail", "Apply fix")
        assert "Failed" in result
        assert hook.state.items[0].status == "failed"

    def test_update_plan_not_in_full_plan_mode(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.DIRECT)
        result = hook.update_plan("add", "something")
        assert "only available in full_plan" in result

    @pytest.mark.asyncio
    async def test_phase_transition_plan_to_execute(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.FULL_PLAN)
        hook._state.current_phase = Phase.PLAN
        hook.update_plan("add", "Fix the bug")

        ctx = _make_context(iteration=2, tool_calls=[_tool_call("edit_file")])
        await hook.after_iteration(ctx)
        assert hook.state.current_phase == Phase.EXECUTE

    @pytest.mark.asyncio
    async def test_confirm_fail_restarts_cycle(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.FULL_PLAN)
        hook._state.current_phase = Phase.CONFIRM

        ctx = _make_context(
            iteration=5,
            tool_calls=[_tool_call("exec")],
            error="tests failed",
        )
        await hook.after_iteration(ctx)
        assert hook.state.current_phase == Phase.INVESTIGATE
        assert hook.state.cycle_count == 2


class TestPersistence:
    @pytest.mark.asyncio
    async def test_saves_to_disk(self, tmp_path):
        hook = PlanHook(workspace=tmp_path, session_key="test_sess")
        hook.set_tier(ExecutionTier.FULL_PLAN, "complex task")
        hook.update_plan("add", "Step 1")

        # Verify files exist
        plan_dir = tmp_path / "plans" / "test_sess"
        assert (plan_dir / "plan.json").exists()
        assert (plan_dir / "events.jsonl").exists()

    @pytest.mark.asyncio
    async def test_loads_existing_state(self, tmp_path):
        hook1 = PlanHook(workspace=tmp_path, session_key="resume")
        hook1.set_tier(ExecutionTier.FULL_PLAN, "task")
        hook1.update_plan("add", "Step A")

        hook2 = PlanHook(workspace=tmp_path, session_key="resume")
        assert hook2.state.tier == ExecutionTier.FULL_PLAN
        assert len(hook2.state.items) == 1
        assert hook2.state.items[0].description == "Step A"
