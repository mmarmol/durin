"""Tests for deliberation integration with PlanHook."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.hook import AgentHookContext
from durin.deliberation.service import DeliberationService
from durin.deliberation.engine import DeliberationEngine
from durin.plan.hook import PlanHook
from durin.plan.types import ExecutionTier, Phase
from durin.providers.base import LLMResponse


_DELIB_RESPONSE = """\
[CRITIC]
Risk: rebinding vs in-place assignment.

[EXPLORER]
Could use np.copyto() instead.

[PRAGMATIC]
Use [...] = for view writes.

[SYNTHESIS]
Fix with in-place assignment to avoid rebinding.
"""


def _make_context(iteration: int = 1, error: str | None = None) -> AgentHookContext:
    return AgentHookContext(
        iteration=iteration,
        messages=[
            {"role": "user", "content": "Fix the numpy view bug in coordinates.py"},
            {"role": "assistant", "content": "I'll investigate the issue."},
            {"role": "tool", "content": "File content showing output_field = output_field.replace(...)"},
        ],
        error=error,
    )


def _simulate_verify_fail(hook: PlanHook) -> None:
    """Simulate cycle 1 fast path failing: edit → exec → verify fail."""
    hook._state.edit_detected = True
    ctx = _make_context(error="AssertionError: expected 'D' exponent")
    hook._on_verify_fail(ctx)


@pytest.fixture
def delib_service():
    provider = AsyncMock()
    provider.chat.return_value = LLMResponse(content=_DELIB_RESPONSE)
    engine = DeliberationEngine(provider=provider, model="glm-5.1")
    return DeliberationService(engine=engine, telemetry=MagicMock())


class TestPlanHookDeliberation:
    def test_no_deliberation_on_cycle_1(self, delib_service):
        """Cycle 1 is fast path — no deliberation."""
        hook = PlanHook(deliberation=delib_service)
        hook.set_tier(ExecutionTier.PLAN)
        assert hook.state.current_phase == Phase.EXECUTE
        assert hook._deliberation_needed is False

    @pytest.mark.asyncio
    async def test_deliberation_fires_after_verify_fail(self, delib_service):
        """Deliberation fires on cycle 2+ when escalating to full plan."""
        hook = PlanHook(deliberation=delib_service)
        hook.set_tier(ExecutionTier.PLAN)

        _simulate_verify_fail(hook)
        assert hook.state.current_phase == Phase.INVESTIGATE
        assert hook.state.cycle_count == 2

        hook.update_plan("add", "Fix the view assignment with [:]")
        assert hook.state.current_phase == Phase.PLAN
        assert hook._deliberation_needed is True

        ctx = _make_context()
        await hook.before_iteration(ctx)

        assert hook._deliberation_needed is False
        injected = [m for m in ctx.messages if m.get("role") == "system"]
        assert any("Pre-analysis deliberation" in m["content"] for m in injected)

    @pytest.mark.asyncio
    async def test_no_deliberation_without_service(self):
        hook = PlanHook(deliberation=None)
        hook.set_tier(ExecutionTier.PLAN)

        _simulate_verify_fail(hook)
        hook.update_plan("add", "Step 1")

        ctx = _make_context()
        await hook.before_iteration(ctx)
        injected = [m for m in ctx.messages if m.get("role") == "system"]
        assert not any("Deliberation" in m.get("content", "") for m in injected)

    @pytest.mark.asyncio
    async def test_deliberation_uses_posture_snapshot(self, delib_service):
        posture_fn = lambda: {"caution": 0.8, "exploration": 0.3}
        hook = PlanHook(deliberation=delib_service, posture_snapshot_fn=posture_fn)
        hook.set_tier(ExecutionTier.PLAN)

        _simulate_verify_fail(hook)
        hook.update_plan("add", "Step 1")

        ctx = _make_context()
        await hook.before_iteration(ctx)

        call = delib_service._engine.provider.chat.call_args
        system_msg = call.kwargs["messages"][0]["content"]
        assert "exhaustive" in system_msg

    @pytest.mark.asyncio
    async def test_deliberation_includes_failure_context(self, delib_service):
        """Deliberation receives the previous failure context."""
        hook = PlanHook(deliberation=delib_service)
        hook.set_tier(ExecutionTier.PLAN)

        _simulate_verify_fail(hook)
        assert hook.state.last_failure_context == "AssertionError: expected 'D' exponent"

        hook.update_plan("add", "Step 1")
        ctx = _make_context()
        await hook.before_iteration(ctx)

        call = delib_service._engine.provider.chat.call_args
        user_msg = call.kwargs["messages"][1]["content"]
        assert "AssertionError" in user_msg

    @pytest.mark.asyncio
    async def test_deliberation_does_not_fire_twice(self, delib_service):
        hook = PlanHook(deliberation=delib_service)
        hook.set_tier(ExecutionTier.PLAN)

        _simulate_verify_fail(hook)
        hook.update_plan("add", "Step 1")

        ctx = _make_context()
        await hook.before_iteration(ctx)
        assert delib_service._engine.provider.chat.call_count == 1

        ctx2 = _make_context(iteration=2)
        await hook.before_iteration(ctx2)
        assert delib_service._engine.provider.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_deliberation_error_does_not_crash(self):
        provider = AsyncMock()
        provider.chat.side_effect = RuntimeError("API timeout")
        engine = DeliberationEngine(provider=provider, model="glm-5.1")
        service = DeliberationService(engine=engine, telemetry=MagicMock())

        hook = PlanHook(deliberation=service)
        hook.set_tier(ExecutionTier.PLAN)

        _simulate_verify_fail(hook)
        hook.update_plan("add", "Step 1")

        ctx = _make_context()
        await hook.before_iteration(ctx)
        assert hook._deliberation_needed is False
