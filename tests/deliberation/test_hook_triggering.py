"""Tests for deliberation hook smart triggering (goal skip, posture drift)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from durin.agent.hook import AgentHookContext
from durin.deliberation.engine import DeliberationEngine
from durin.deliberation.evaluator import LLMEvaluator
from durin.deliberation.generator import GeneratorConfig
from durin.deliberation.hook import DeliberationHook
from durin.deliberation.types import GeneratorRole
from durin.providers.base import LLMResponse


def _mock_provider(responses: list[str]):
    provider = AsyncMock()
    call_count = [0]

    async def _chat(**kwargs):
        idx = call_count[0] % len(responses)
        call_count[0] += 1
        return LLMResponse(
            content=responses[idx], tool_calls=[], finish_reason="stop", usage={},
        )

    provider.chat = _chat
    return provider


def _make_engine(provider):
    generators = [
        GeneratorConfig(role=GeneratorRole.PRAGMATICO, model="m", temperature=0.3, prompt_template="t"),
        GeneratorConfig(role=GeneratorRole.EXPLORADOR, model="m", temperature=0.8, prompt_template="t"),
        GeneratorConfig(role=GeneratorRole.CRITICO, model="m", temperature=0.5, prompt_template="t"),
    ]
    evaluators = [
        LLMEvaluator("avance", provider, "m", "score"),
        LLMEvaluator("reversibilidad", provider, "m", "score"),
    ]
    return DeliberationEngine(
        provider=provider, generators=generators,
        evaluators=evaluators, max_rounds=1,
    )


def _ctx(iteration=0, goal_active=False):
    system_content = "You are a helpful assistant."
    if goal_active:
        system_content += "\n\nGoal (active):\nImplement login system"
    return AgentHookContext(
        iteration=iteration,
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": "implementar auth"},
        ],
    )


class TestGoalActiveSkip:
    @pytest.mark.asyncio
    async def test_skips_deliberation_when_goal_active(self):
        responses = ["x"] * 20
        provider = _mock_provider(responses)
        engine = _make_engine(provider)
        hook = DeliberationHook(engine=engine)

        ctx = _ctx(iteration=0, goal_active=True)
        await hook.before_iteration(ctx)

        assert hook.last_verdict is None

    @pytest.mark.asyncio
    async def test_deliberates_when_no_goal(self):
        responses = ["usar JWT", "explorar passkeys", "OAuth2", "7", "8", "6", "5", "7", "9"]
        provider = _mock_provider(responses)
        engine = _make_engine(provider)
        hook = DeliberationHook(engine=engine)

        ctx = _ctx(iteration=0, goal_active=False)
        await hook.before_iteration(ctx)

        assert hook.last_verdict is not None


class TestPostureDriftRedeliberation:
    @pytest.mark.asyncio
    async def test_redeliberates_when_posture_drifts(self):
        responses = ["usar JWT", "explorar", "OAuth2", "7", "8", "6", "5", "7", "9"] * 3
        provider = _mock_provider(responses)
        engine = _make_engine(provider)

        posture_value = [0.5]

        def snapshot_fn():
            return {"cautela": posture_value[0], "exploracion": 0.4}

        hook = DeliberationHook(
            engine=engine,
            posture_snapshot_fn=snapshot_fn,
            drift_threshold=0.15,
        )

        # First deliberation at iteration 0
        ctx = _ctx(iteration=0)
        await hook.before_iteration(ctx)
        assert hook.last_verdict is not None
        first_verdict = hook.last_verdict

        # Simulate posture drift
        posture_value[0] = 0.8  # +0.3, exceeds 0.15 threshold

        # Iteration 5 — should re-deliberate due to drift
        ctx2 = _ctx(iteration=5)
        await hook.before_iteration(ctx2)
        # A new deliberation happened
        assert hook.last_verdict is not first_verdict

    @pytest.mark.asyncio
    async def test_no_redeliberation_without_drift(self):
        responses = ["usar JWT", "explorar", "OAuth2", "7", "8", "6", "5", "7", "9"]
        provider = _mock_provider(responses)
        engine = _make_engine(provider)

        def snapshot_fn():
            return {"cautela": 0.5, "exploracion": 0.4}

        hook = DeliberationHook(
            engine=engine,
            posture_snapshot_fn=snapshot_fn,
            drift_threshold=0.15,
        )

        # First deliberation
        ctx = _ctx(iteration=0)
        await hook.before_iteration(ctx)
        first_verdict = hook.last_verdict

        # Iteration 5 — posture hasn't moved, should NOT re-deliberate
        ctx2 = _ctx(iteration=5)
        await hook.before_iteration(ctx2)
        assert hook.last_verdict is first_verdict

    @pytest.mark.asyncio
    async def test_no_redeliberation_without_initial_deliberation(self):
        responses = ["x"] * 20
        provider = _mock_provider(responses)
        engine = _make_engine(provider)

        def snapshot_fn():
            return {"cautela": 0.9}

        hook = DeliberationHook(
            engine=engine,
            posture_snapshot_fn=snapshot_fn,
            drift_threshold=0.15,
        )

        # Skip initial (goal active), then check iteration 5
        ctx = _ctx(iteration=0, goal_active=True)
        await hook.before_iteration(ctx)
        assert hook.last_verdict is None

        # No baseline → _should_redeliberate returns False
        ctx2 = _ctx(iteration=5)
        await hook.before_iteration(ctx2)
        assert hook.last_verdict is None
