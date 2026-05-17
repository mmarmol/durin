"""Tests for DeliberationHook."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from durin.agent.hook import AgentHookContext, CompositeHook
from durin.deliberation.engine import DeliberationEngine
from durin.deliberation.hook import DeliberationHook
from durin.deliberation.types import (
    GeneratorRole,
    Proposal,
    ScoredProposal,
    Verdict,
)
from durin.providers.base import ToolCallRequest


def _make_context(
    *,
    iteration: int = 0,
    tool_calls: list | None = None,
    messages: list | None = None,
) -> AgentHookContext:
    return AgentHookContext(
        iteration=iteration,
        messages=messages or [
            {"role": "system", "content": "You are an agent."},
            {"role": "user", "content": "do something"},
        ],
        tool_calls=tool_calls or [],
    )


def _mock_engine(accepted: bool = True) -> AsyncMock:
    engine = AsyncMock(spec=DeliberationEngine)
    sp = ScoredProposal(
        proposal=Proposal(role=GeneratorRole.PRAGMATICO, content="do it directly", round_number=1),
        scores=(),
        final_score=0.7,
    )
    engine.deliberate.return_value = Verdict(
        winner=sp,
        accepted=accepted,
        threshold=0.55,
        all_proposals=(sp,),
        rounds_used=1,
        under_doubt=False,
    )
    return engine


class TestPlanningMomentTiming:
    """PLANNING_MOMENT fires in before_iteration(0), BEFORE the LLM call."""

    @pytest.mark.asyncio
    async def test_triggers_on_first_iteration(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)
        engine.deliberate.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_trigger_on_later_iterations(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        ctx = _make_context(iteration=3)
        await hook.before_iteration(ctx)
        engine.deliberate.assert_not_called()

    @pytest.mark.asyncio
    async def test_injects_synthesis_as_pre_message(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)

        delib_msgs = [m for m in ctx.messages if "Deliberación pre-análisis" in m.get("content", "")]
        assert len(delib_msgs) == 1
        assert "do it directly" in delib_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_deliberation_inserted_before_user_message(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)

        # System prompt remains untouched
        assert ctx.messages[0]["content"] == "You are an agent."
        # Deliberation message is before the user message
        user_idx = next(i for i, m in enumerate(ctx.messages) if m.get("role") == "user")
        delib_idx = next(i for i, m in enumerate(ctx.messages) if "Deliberación pre-análisis" in m.get("content", ""))
        assert delib_idx < user_idx


class TestCriticalActionTiming:
    """CRITICAL_ACTION fires in before_execute_tools on non-zero iterations."""

    @pytest.mark.asyncio
    async def test_triggers_on_critical_tool(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        ctx = _make_context(
            iteration=5,
            tool_calls=[ToolCallRequest(id="1", name="exec", arguments="{}")],
        )
        await hook.before_execute_tools(ctx)
        engine.deliberate.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_trigger_on_safe_tool(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        ctx = _make_context(
            iteration=5,
            tool_calls=[ToolCallRequest(id="1", name="read_file", arguments="{}")],
        )
        await hook.before_execute_tools(ctx)
        engine.deliberate.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_trigger_on_iteration_zero(self):
        """Iteration 0 is handled by before_iteration, not before_execute_tools."""
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        ctx = _make_context(
            iteration=0,
            tool_calls=[ToolCallRequest(id="1", name="exec", arguments="{}")],
        )
        await hook.before_execute_tools(ctx)
        engine.deliberate.assert_not_called()

    @pytest.mark.asyncio
    async def test_injects_deliberation_message(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        ctx = _make_context(
            iteration=3,
            tool_calls=[ToolCallRequest(id="1", name="shell", arguments="{}")],
        )
        await hook.before_execute_tools(ctx)
        delib_msgs = [m for m in ctx.messages if "Deliberación pre-análisis" in m.get("content", "")]
        assert len(delib_msgs) == 1


class TestHookBehavior:
    @pytest.mark.asyncio
    async def test_stores_last_verdict(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        assert hook.last_verdict is None

        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)
        assert hook.last_verdict is not None
        assert hook.last_verdict.accepted is True

    @pytest.mark.asyncio
    async def test_engine_failure_graceful(self):
        engine = AsyncMock(spec=DeliberationEngine)
        engine.deliberate.side_effect = RuntimeError("ollama down")
        hook = DeliberationHook(engine)

        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)
        assert hook.last_verdict is None
        # System prompt unchanged
        assert "# Deliberación" not in ctx.messages[0]["content"]

    @pytest.mark.asyncio
    async def test_posture_snapshot_fn_called(self):
        engine = _mock_engine()
        snapshot_fn = lambda: {"cautela": 0.8, "profundidad": 0.6}
        hook = DeliberationHook(engine, posture_snapshot_fn=snapshot_fn)

        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)

        call_args = engine.deliberate.call_args[0][0]
        assert call_args.posture_snapshot == {"cautela": 0.8, "profundidad": 0.6}

    @pytest.mark.asyncio
    async def test_extracts_goal_from_messages(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        ctx = _make_context(
            iteration=0,
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "deploy to production"},
            ],
        )
        await hook.before_iteration(ctx)
        call_args = engine.deliberate.call_args[0][0]
        assert "deploy to production" in call_args.goal_summary

    @pytest.mark.asyncio
    async def test_replaces_previous_deliberation_message(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)

        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)

        # Simulate second deliberation on critical action
        ctx.iteration = 3
        ctx.tool_calls = [ToolCallRequest(id="1", name="exec", arguments="{}")]

        # Change engine response
        sp2 = ScoredProposal(
            proposal=Proposal(role=GeneratorRole.CRITICO, content="safer path", round_number=1),
            scores=(),
            final_score=0.8,
        )
        engine.deliberate.return_value = Verdict(
            winner=sp2, accepted=True, threshold=0.55,
            all_proposals=(sp2,), rounds_used=1, under_doubt=False,
        )
        await hook.before_execute_tools(ctx)

        # Should have exactly one deliberation message
        delib_msgs = [m for m in ctx.messages if "Deliberación pre-análisis" in m.get("content", "")]
        assert len(delib_msgs) == 1
        assert "safer path" in delib_msgs[0]["content"]
        assert "do it directly" not in delib_msgs[0]["content"]


class TestSynthesisExposure:
    @pytest.mark.asyncio
    async def test_synthesis_available_after_deliberation(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        assert hook.last_synthesis is None

        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)
        assert hook.last_synthesis is not None
        assert isinstance(hook.last_synthesis, str)
        assert len(hook.last_synthesis) > 0

    @pytest.mark.asyncio
    async def test_synthesis_contains_proposal_content(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)

        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)
        assert "do it directly" in hook.last_synthesis

    @pytest.mark.asyncio
    async def test_synthesis_none_on_engine_failure(self):
        engine = AsyncMock(spec=DeliberationEngine)
        engine.deliberate.side_effect = RuntimeError("down")
        hook = DeliberationHook(engine)

        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)
        assert hook.last_synthesis is None


class TestDynamicDriftThreshold:
    @pytest.mark.asyncio
    async def test_cautela_high_lowers_threshold(self):
        engine = _mock_engine()
        snapshot = {"cautela": 0.9, "exploracion": 0.5, "profundidad": 0.5}
        hook = DeliberationHook(engine, posture_snapshot_fn=lambda: snapshot)
        threshold = hook._effective_drift_threshold(snapshot)
        # 0.15 - 0.05 * (0.9 - 0.5) = 0.15 - 0.02 = 0.13
        assert threshold == pytest.approx(0.13)

    @pytest.mark.asyncio
    async def test_cautela_low_raises_threshold(self):
        engine = _mock_engine()
        snapshot = {"cautela": 0.1, "exploracion": 0.5, "profundidad": 0.5}
        hook = DeliberationHook(engine, posture_snapshot_fn=lambda: snapshot)
        threshold = hook._effective_drift_threshold(snapshot)
        # 0.15 - 0.05 * (0.1 - 0.5) = 0.15 + 0.02 = 0.17
        assert threshold == pytest.approx(0.17)

    @pytest.mark.asyncio
    async def test_cautela_neutral_unchanged(self):
        engine = _mock_engine()
        snapshot = {"cautela": 0.5, "exploracion": 0.5, "profundidad": 0.5}
        hook = DeliberationHook(engine, posture_snapshot_fn=lambda: snapshot)
        threshold = hook._effective_drift_threshold(snapshot)
        assert threshold == pytest.approx(0.15)

    @pytest.mark.asyncio
    async def test_redeliberation_easier_with_high_cautela(self):
        engine = _mock_engine()
        snapshot = {"cautela": 0.9, "exploracion": 0.5, "profundidad": 0.5}
        hook = DeliberationHook(engine, posture_snapshot_fn=lambda: snapshot)

        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)

        # Simulate drift of 0.13 (below static 0.15 but above dynamic 0.13)
        hook._posture_at_last_deliberation = {"cautela": 0.77, "exploracion": 0.5, "profundidad": 0.5}
        # Current snapshot has cautela 0.9, drift = 0.13
        assert hook._should_redeliberate() is True

    @pytest.mark.asyncio
    async def test_no_redeliberation_with_low_cautela_same_drift(self):
        engine = _mock_engine()
        snapshot = {"cautela": 0.1, "exploracion": 0.5, "profundidad": 0.5}
        hook = DeliberationHook(engine, posture_snapshot_fn=lambda: snapshot)

        ctx = _make_context(iteration=0)
        await hook.before_iteration(ctx)

        # Same drift of 0.13, but low cautela means threshold is 0.17
        hook._posture_at_last_deliberation = {"cautela": 0.23, "exploracion": 0.5, "profundidad": 0.5}
        # drift = |0.1 - 0.23| = 0.13 < 0.17 → no redeliberation
        assert hook._should_redeliberate() is False


class TestCompositeIntegration:
    @pytest.mark.asyncio
    async def test_hook_in_composite(self):
        engine = _mock_engine()
        hook = DeliberationHook(engine)
        composite = CompositeHook([hook])

        ctx = _make_context(iteration=0)
        await composite.before_iteration(ctx)
        assert hook.last_verdict is not None
        delib_msgs = [m for m in ctx.messages if "Deliberación pre-análisis" in m.get("content", "")]
        assert len(delib_msgs) == 1
