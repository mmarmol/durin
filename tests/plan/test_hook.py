"""Tests for PlanHook — 2-tier execution model with forced verification."""

import pytest

from durin.agent.hook import AgentHookContext
from durin.plan.hook import PlanHook
from durin.plan.types import ExecutionTier, Phase, PlanItem, PHASE_TEMPERATURE
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

    def test_set_tier_plan_initializes_cycle(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN, "complex refactoring")
        assert hook.state.tier == ExecutionTier.PLAN
        assert hook.state.current_phase == Phase.EXECUTE
        assert hook.state.cycle_count == 1


class TestDirectTier:
    @pytest.mark.asyncio
    async def test_no_injection_after_tier_set(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.DIRECT)
        ctx = _make_context(iteration=1)
        await hook.before_iteration(ctx)
        assert ctx.injected_messages_count == 0

    def test_can_complete_always_allowed(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.DIRECT)
        allowed, reason = hook.can_complete()
        assert allowed is True


class TestPlanTier:
    @pytest.mark.asyncio
    async def test_injects_phase_prompt(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN, "multi-step fix")
        ctx = _make_context(iteration=1)
        await hook.before_iteration(ctx)
        assert ctx.injected_messages_count == 1
        system_msgs = [m for m in ctx.messages if m["role"] == "system"]
        assert any("Direct Fix" in m["content"] for m in system_msgs)

    @pytest.mark.asyncio
    async def test_sets_temperature_override(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN, "fix")
        ctx = _make_context(iteration=1)
        await hook.before_iteration(ctx)
        assert ctx.temperature_override == PHASE_TEMPERATURE[Phase.EXECUTE]

    def test_update_plan_add(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        result = hook.update_plan("add", "Read fitsrec.py")
        assert "Added" in result
        assert len(hook.state.items) == 1
        assert hook.state.items[0].description == "Read fitsrec.py"

    def test_update_plan_complete(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook.update_plan("add", "Read file")
        result = hook.update_plan("complete", "Read file")
        assert "Completed" in result
        assert hook.state.items[0].status == "done"

    def test_update_plan_fail(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook.update_plan("add", "Apply fix")
        result = hook.update_plan("fail", "Apply fix")
        assert "Failed" in result
        assert hook.state.items[0].status == "failed"

    def test_update_plan_not_in_plan_mode(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.DIRECT)
        result = hook.update_plan("add", "something")
        assert "only available in plan mode" in result

    @pytest.mark.asyncio
    async def test_phase_transition_plan_to_execute(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook._state.current_phase = Phase.PLAN
        hook.update_plan("add", "Fix the bug")

        ctx = _make_context(iteration=2, tool_calls=[_tool_call("edit_file")])
        await hook.after_iteration(ctx)
        assert hook.state.current_phase == Phase.EXECUTE

    @pytest.mark.asyncio
    async def test_phase_transition_execute_to_verify(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook._state.current_phase = Phase.EXECUTE
        hook._edit_detected = True

        ctx = _make_context(iteration=3, tool_calls=[_tool_call("exec")])
        await hook.after_iteration(ctx)
        assert hook.state.current_phase == Phase.VERIFY

    @pytest.mark.asyncio
    async def test_verify_fail_restarts_cycle(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook._state.current_phase = Phase.VERIFY
        hook._edit_detected = True

        ctx = _make_context(
            iteration=5,
            tool_calls=[_tool_call("exec")],
            error="tests failed",
        )
        await hook.after_iteration(ctx)
        assert hook.state.current_phase == Phase.INVESTIGATE
        assert hook.state.cycle_count == 2
        assert "verify_fail" in ctx.external_stimulus_events

    @pytest.mark.asyncio
    async def test_verify_pass_allows_completion(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook._state.current_phase = Phase.VERIFY
        hook._edit_detected = True
        hook._state.edit_detected = True

        ctx = _make_context(
            iteration=5,
            tool_calls=[_tool_call("exec")],
        )
        await hook.after_iteration(ctx)
        assert hook.state.verify_passed is True
        allowed, _ = hook.can_complete()
        assert allowed is True


class TestForcedVerification:
    def test_cannot_complete_after_edit_without_verify(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook._state.edit_detected = True
        hook._state.verify_passed = False

        allowed, reason = hook.can_complete()
        assert allowed is False
        assert "verify" in reason.lower()

    def test_can_complete_after_successful_verify(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook._state.edit_detected = True
        hook._state.verify_passed = True

        allowed, reason = hook.can_complete()
        assert allowed is True

    def test_can_complete_if_no_edits_made(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook._state.edit_detected = False

        allowed, reason = hook.can_complete()
        assert allowed is True


class TestRetryEvaluation:
    @pytest.mark.asyncio
    async def test_injects_self_eval_on_cycle_2(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook._state.current_phase = Phase.PLAN
        hook._state.cycle_count = 2
        hook._state.last_failure_context = "AssertionError: expected 1 got 2"

        ctx = _make_context(iteration=10)
        await hook.before_iteration(ctx)
        system_msgs = [m for m in ctx.messages if m["role"] == "system"]
        content = system_msgs[0]["content"]
        assert "FAILED verification" in content
        assert "AssertionError" in content
        assert "genuinely DIFFERENT" in content


class TestTemperature:
    @pytest.mark.asyncio
    async def test_investigate_temperature(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook._state.current_phase = Phase.INVESTIGATE
        ctx = _make_context(iteration=1)
        await hook.before_iteration(ctx)
        assert ctx.temperature_override == 0.5

    @pytest.mark.asyncio
    async def test_execute_temperature(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook._state.current_phase = Phase.EXECUTE
        ctx = _make_context(iteration=3)
        await hook.before_iteration(ctx)
        assert ctx.temperature_override == 0.15

    @pytest.mark.asyncio
    async def test_verify_temperature(self):
        hook = PlanHook()
        hook.set_tier(ExecutionTier.PLAN)
        hook._state.current_phase = Phase.VERIFY
        ctx = _make_context(iteration=4)
        await hook.before_iteration(ctx)
        assert ctx.temperature_override == 0.1


class TestPersistence:
    @pytest.mark.asyncio
    async def test_saves_to_disk(self, tmp_path):
        hook = PlanHook(workspace=tmp_path, session_key="test_sess")
        hook.set_tier(ExecutionTier.PLAN, "complex task")
        hook.update_plan("add", "Step 1")

        plan_dir = tmp_path / "plans" / "test_sess"
        assert (plan_dir / "plan.json").exists()
        assert (plan_dir / "events.jsonl").exists()

    @pytest.mark.asyncio
    async def test_loads_existing_state(self, tmp_path):
        hook1 = PlanHook(workspace=tmp_path, session_key="resume")
        hook1.set_tier(ExecutionTier.PLAN, "task")
        hook1.update_plan("add", "Step A")

        hook2 = PlanHook(workspace=tmp_path, session_key="resume")
        assert hook2.state.tier == ExecutionTier.PLAN
        assert len(hook2.state.items) == 1
        assert hook2.state.items[0].description == "Step A"
