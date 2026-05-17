"""Tests for advanced stimulus event detection in PostureHook."""

from __future__ import annotations

import pytest

from durin.agent.hook import AgentHookContext
from durin.posture.hook import PostureHook
from durin.posture.stimulus import StimulusEvent, StimulusTable
from durin.posture.vector import AxisName, AxisState, PostureVector
from durin.providers.base import ToolCallRequest


def _make_vector() -> PostureVector:
    axes = {
        name: AxisState(mean=0.5, variance=0.15, return_force=0.3, current_value=0.5)
        for name in AxisName
    }
    return PostureVector(axes=axes)


def _make_hook() -> PostureHook:
    return PostureHook(vector=_make_vector())


def _ctx(
    iteration: int = 1,
    tool_calls: list[ToolCallRequest] | None = None,
    final_content: str | None = None,
    error: str | None = None,
    injected_messages_count: int = 0,
) -> AgentHookContext:
    return AgentHookContext(
        iteration=iteration,
        messages=[{"role": "system", "content": "test"}],
        tool_calls=tool_calls or [],
        final_content=final_content,
        error=error,
        injected_messages_count=injected_messages_count,
    )


def _tool_call(name: str) -> ToolCallRequest:
    return ToolCallRequest(id="t1", name=name, arguments="{}")


class TestUserCorrected:
    def test_fires_on_injection(self):
        hook = _make_hook()
        ctx = _ctx(injected_messages_count=2)
        events = hook._detect_events(ctx)
        assert StimulusEvent.USER_CORRECTED in events

    def test_not_fired_without_injection(self):
        hook = _make_hook()
        ctx = _ctx(injected_messages_count=0, tool_calls=[_tool_call("read_file")])
        events = hook._detect_events(ctx)
        assert StimulusEvent.USER_CORRECTED not in events

    @pytest.mark.asyncio
    async def test_conformidad_increases_on_user_correction(self):
        hook = _make_hook()
        before = hook.current_vector.snapshot()[AxisName.CONFORMITY]
        ctx = _ctx(injected_messages_count=1, tool_calls=[_tool_call("read_file")])
        await hook.after_iteration(ctx)
        after = hook.current_vector.snapshot()[AxisName.CONFORMITY]
        assert after > before


class TestGoalAmbiguous:
    def test_fires_on_empty_iteration(self):
        hook = _make_hook()
        ctx = _ctx(iteration=3, tool_calls=[], final_content=None, error=None)
        events = hook._detect_events(ctx)
        assert StimulusEvent.GOAL_AMBIGUOUS in events

    def test_not_fired_on_iteration_zero(self):
        hook = _make_hook()
        ctx = _ctx(iteration=0, tool_calls=[], final_content=None, error=None)
        events = hook._detect_events(ctx)
        assert StimulusEvent.GOAL_AMBIGUOUS not in events

    def test_not_fired_when_tools_present(self):
        hook = _make_hook()
        ctx = _ctx(iteration=2, tool_calls=[_tool_call("read_file")], final_content=None)
        events = hook._detect_events(ctx)
        assert StimulusEvent.GOAL_AMBIGUOUS not in events

    def test_not_fired_when_content_present(self):
        hook = _make_hook()
        ctx = _ctx(iteration=2, tool_calls=[], final_content="some output")
        events = hook._detect_events(ctx)
        assert StimulusEvent.GOAL_AMBIGUOUS not in events

    def test_not_fired_when_error_present(self):
        hook = _make_hook()
        ctx = _ctx(iteration=2, tool_calls=[], final_content=None, error="timeout")
        events = hook._detect_events(ctx)
        assert StimulusEvent.GOAL_AMBIGUOUS not in events

    @pytest.mark.asyncio
    async def test_profundidad_increases_on_ambiguity(self):
        hook = _make_hook()
        before = hook.current_vector.snapshot()[AxisName.DEPTH]
        ctx = _ctx(iteration=3, tool_calls=[], final_content=None, error=None)
        await hook.after_iteration(ctx)
        after = hook.current_vector.snapshot()[AxisName.DEPTH]
        assert after > before


class TestCriticalAction:
    def test_fires_on_exec(self):
        hook = _make_hook()
        ctx = _ctx(tool_calls=[_tool_call("exec")])
        events = hook._detect_events(ctx)
        assert StimulusEvent.CRITICAL_ACTION in events

    def test_fires_on_shell(self):
        hook = _make_hook()
        ctx = _ctx(tool_calls=[_tool_call("shell")])
        events = hook._detect_events(ctx)
        assert StimulusEvent.CRITICAL_ACTION in events

    def test_fires_on_deploy(self):
        hook = _make_hook()
        ctx = _ctx(tool_calls=[_tool_call("deploy")])
        events = hook._detect_events(ctx)
        assert StimulusEvent.CRITICAL_ACTION in events

    def test_not_fired_on_read_file(self):
        hook = _make_hook()
        ctx = _ctx(tool_calls=[_tool_call("read_file")])
        events = hook._detect_events(ctx)
        assert StimulusEvent.CRITICAL_ACTION not in events

    @pytest.mark.asyncio
    async def test_cautela_increases_on_critical_action(self):
        hook = _make_hook()
        before = hook.current_vector.snapshot()[AxisName.CAUTION]
        ctx = _ctx(tool_calls=[_tool_call("exec")])
        await hook.after_iteration(ctx)
        after = hook.current_vector.snapshot()[AxisName.CAUTION]
        assert after > before


class TestMultipleEventsCoexist:
    def test_injection_and_critical_action(self):
        hook = _make_hook()
        ctx = _ctx(
            injected_messages_count=1,
            tool_calls=[_tool_call("shell")],
        )
        events = hook._detect_events(ctx)
        assert StimulusEvent.USER_CORRECTED in events
        assert StimulusEvent.CRITICAL_ACTION in events
        assert StimulusEvent.STEP_SUCCEEDED in events

    def test_no_interference_with_step_failed(self):
        hook = _make_hook()
        ctx = _ctx(tool_calls=[_tool_call("exec")], error="boom")
        events = hook._detect_events(ctx)
        assert StimulusEvent.STEP_FAILED in events
        assert StimulusEvent.CRITICAL_ACTION in events


class TestExplicitProtocol:
    def _ctx_with_system(self, system_content: str) -> AgentHookContext:
        return AgentHookContext(
            iteration=1,
            messages=[{"role": "system", "content": system_content}],
            tool_calls=[_tool_call("read_file")],
            final_content="output",
        )

    def test_fires_on_steps_header(self):
        hook = _make_hook()
        ctx = self._ctx_with_system("Some preamble\n\n## Steps\n\n1. Do X\n2. Do Y")
        events = hook._detect_events(ctx)
        assert StimulusEvent.EXPLICIT_PROTOCOL in events

    def test_fires_on_checklist_header(self):
        hook = _make_hook()
        ctx = self._ctx_with_system("Context\n\n## Checklist\n\n- [ ] First")
        events = hook._detect_events(ctx)
        assert StimulusEvent.EXPLICIT_PROTOCOL in events

    def test_fires_on_procedure_header(self):
        hook = _make_hook()
        ctx = self._ctx_with_system("## Procedure\n\nFollow these steps")
        events = hook._detect_events(ctx)
        assert StimulusEvent.EXPLICIT_PROTOCOL in events

    def test_fires_on_step_1(self):
        hook = _make_hook()
        ctx = self._ctx_with_system("Instructions: step 1, do X")
        events = hook._detect_events(ctx)
        assert StimulusEvent.EXPLICIT_PROTOCOL in events

    def test_not_fired_on_normal_prompt(self):
        hook = _make_hook()
        ctx = self._ctx_with_system("You are a helpful assistant.")
        events = hook._detect_events(ctx)
        assert StimulusEvent.EXPLICIT_PROTOCOL not in events

    def test_case_insensitive(self):
        hook = _make_hook()
        ctx = self._ctx_with_system("## STEPS\n\n1. Do it")
        events = hook._detect_events(ctx)
        assert StimulusEvent.EXPLICIT_PROTOCOL in events

    @pytest.mark.asyncio
    async def test_disciplina_increases_on_protocol(self):
        hook = _make_hook()
        before = hook.current_vector.snapshot()[AxisName.DISCIPLINE]
        ctx = AgentHookContext(
            iteration=1,
            messages=[{"role": "system", "content": "## Steps\n\n1. Do X"}],
            tool_calls=[_tool_call("read_file")],
            final_content="done",
        )
        await hook.after_iteration(ctx)
        after = hook.current_vector.snapshot()[AxisName.DISCIPLINE]
        assert after > before

    @pytest.mark.asyncio
    async def test_fires_only_once_per_session(self):
        hook = _make_hook()
        ctx = AgentHookContext(
            iteration=1,
            messages=[{"role": "system", "content": "## Steps\n\n1. Do X"}],
            tool_calls=[_tool_call("read_file")],
            final_content="done",
        )
        await hook.after_iteration(ctx)
        after_first = hook.current_vector.snapshot()[AxisName.DISCIPLINE]

        # Second iteration with same system prompt — should NOT fire again
        ctx2 = AgentHookContext(
            iteration=2,
            messages=[{"role": "system", "content": "## Steps\n\n1. Do X"}],
            tool_calls=[_tool_call("read_file")],
            final_content="done",
        )
        await hook.after_iteration(ctx2)
        after_second = hook.current_vector.snapshot()[AxisName.DISCIPLINE]

        # Disciplina should not have increased again (only return-to-mean effects)
        assert after_second <= after_first
