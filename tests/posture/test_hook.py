"""Tests for PostureHook lifecycle integration."""

from __future__ import annotations

import pytest

from durin.agent.hook import AgentHookContext, CompositeHook
from durin.posture.hook import PostureHook
from durin.posture.stimulus import StimulusEvent, StimulusTable
from durin.posture.vector import AxisName, PostureVector
from durin.providers.base import ToolCallRequest


def _make_context(
    *,
    iteration: int = 0,
    error: str | None = None,
    tool_calls: list | None = None,
    tool_results: list | None = None,
) -> AgentHookContext:
    return AgentHookContext(
        iteration=iteration,
        messages=[],
        error=error,
        tool_calls=tool_calls or [],
        tool_results=tool_results or [],
    )


def _make_tool_call(name: str = "test_tool") -> ToolCallRequest:
    return ToolCallRequest(id="tc_1", name=name, arguments="{}")


class TestPostureHookBasics:
    def test_initial_phrase_from_default_vector(self):
        hook = PostureHook(PostureVector.default())
        phrase = hook.current_phrase
        assert isinstance(phrase, str)

    def test_current_vector_returns_initial_state(self):
        v = PostureVector.default()
        hook = PostureHook(v)
        assert hook.current_vector == v


class TestEventDetection:
    @pytest.mark.asyncio
    async def test_error_triggers_step_failed(self):
        hook = PostureHook(PostureVector.default())
        ctx = _make_context(error="something broke")
        await hook.after_iteration(ctx)
        assert hook.current_vector.axes[AxisName.CAUTELA].valor_actual > PostureVector.default().axes[AxisName.CAUTELA].valor_actual

    @pytest.mark.asyncio
    async def test_tool_error_result_triggers_step_failed(self):
        hook = PostureHook(PostureVector.default())
        ctx = _make_context(
            tool_calls=[_make_tool_call()],
            tool_results=[{"error": "file not found"}],
        )
        await hook.after_iteration(ctx)
        assert hook.current_vector.axes[AxisName.CAUTELA].valor_actual > PostureVector.default().axes[AxisName.CAUTELA].valor_actual

    @pytest.mark.asyncio
    async def test_is_error_flag_triggers_step_failed(self):
        hook = PostureHook(PostureVector.default())
        ctx = _make_context(
            tool_calls=[_make_tool_call()],
            tool_results=[{"is_error": True, "output": "failed"}],
        )
        await hook.after_iteration(ctx)
        assert hook.current_vector.axes[AxisName.CAUTELA].valor_actual > PostureVector.default().axes[AxisName.CAUTELA].valor_actual

    @pytest.mark.asyncio
    async def test_successful_tool_call_triggers_step_succeeded(self):
        hook = PostureHook(PostureVector.default())
        ctx = _make_context(
            tool_calls=[_make_tool_call()],
            tool_results=[{"output": "ok"}],
        )
        await hook.after_iteration(ctx)
        assert hook.current_vector.axes[AxisName.CAUTELA].valor_actual < PostureVector.default().axes[AxisName.CAUTELA].valor_actual

    @pytest.mark.asyncio
    async def test_no_tool_calls_no_events(self):
        hook = PostureHook(PostureVector.default())
        ctx = _make_context(tool_calls=[], tool_results=[])
        await hook.after_iteration(ctx)
        assert hook.current_vector == PostureVector.default()


class TestConsecutiveTracking:
    @pytest.mark.asyncio
    async def test_three_failures_triggers_consecutive(self):
        hook = PostureHook(PostureVector.default())
        initial_cautela = hook.current_vector.axes[AxisName.CAUTELA].valor_actual

        for _ in range(3):
            ctx = _make_context(error="fail")
            await hook.after_iteration(ctx)

        final_cautela = hook.current_vector.axes[AxisName.CAUTELA].valor_actual
        single_step_delta = 0.10
        consecutive_delta = 0.15
        assert final_cautela > initial_cautela

    @pytest.mark.asyncio
    async def test_success_resets_failure_counter(self):
        hook = PostureHook(PostureVector.default())

        await hook.after_iteration(_make_context(error="fail"))
        await hook.after_iteration(_make_context(error="fail"))
        await hook.after_iteration(_make_context(
            tool_calls=[_make_tool_call()],
            tool_results=[{"output": "ok"}],
        ))
        await hook.after_iteration(_make_context(error="fail"))
        await hook.after_iteration(_make_context(error="fail"))

        # Should NOT have triggered CONSECUTIVE_FAILURES_3 — reset after success
        # Check that conformidad didn't get the -0.10 from CONSECUTIVE_FAILURES_3
        default_conf = PostureVector.default().axes[AxisName.CONFORMIDAD].valor_actual
        actual_conf = hook.current_vector.axes[AxisName.CONFORMIDAD].valor_actual
        # With 4 failures and 1 success, but no 3-consecutive, conformidad should stay at default
        # (only CONSECUTIVE_FAILURES_3 affects conformidad with -0.10)
        assert actual_conf >= default_conf - 0.01

    @pytest.mark.asyncio
    async def test_three_successes_triggers_consecutive(self):
        hook = PostureHook(PostureVector.default())
        initial_exp = hook.current_vector.axes[AxisName.EXPLORACION].valor_actual

        for _ in range(3):
            ctx = _make_context(
                tool_calls=[_make_tool_call()],
                tool_results=[{"output": "ok"}],
            )
            await hook.after_iteration(ctx)

        final_exp = hook.current_vector.axes[AxisName.EXPLORACION].valor_actual
        assert final_exp > initial_exp

    @pytest.mark.asyncio
    async def test_failure_resets_success_counter(self):
        hook = PostureHook(PostureVector.default())

        for _ in range(2):
            await hook.after_iteration(_make_context(
                tool_calls=[_make_tool_call()],
                tool_results=[{"output": "ok"}],
            ))

        await hook.after_iteration(_make_context(error="fail"))

        for _ in range(2):
            await hook.after_iteration(_make_context(
                tool_calls=[_make_tool_call()],
                tool_results=[{"output": "ok"}],
            ))

        # Should NOT have triggered CONSECUTIVE_SUCCESSES_3 — reset after failure
        default_exp = PostureVector.default().axes[AxisName.EXPLORACION].valor_actual
        actual_exp = hook.current_vector.axes[AxisName.EXPLORACION].valor_actual
        # Without the +0.05 from CONSECUTIVE_SUCCESSES_3, exploracion stays near default
        # (no event directly raises exploracion except CONSECUTIVE_SUCCESSES_3 and EXPLORATORY_TASK)
        assert actual_exp <= default_exp + 0.01


class TestCompositeHookIntegration:
    @pytest.mark.asyncio
    async def test_posture_hook_in_composite_works(self):
        hook = PostureHook(PostureVector.default())
        composite = CompositeHook([hook])

        ctx = _make_context(error="fail")
        await composite.after_iteration(ctx)

        assert hook.current_vector.axes[AxisName.CAUTELA].valor_actual > PostureVector.default().axes[AxisName.CAUTELA].valor_actual

    @pytest.mark.asyncio
    async def test_posture_hook_does_not_crash_composite(self):
        hook = PostureHook(PostureVector.default())
        composite = CompositeHook([hook])

        ctx = _make_context()
        await composite.before_iteration(ctx)
        await composite.after_iteration(ctx)
