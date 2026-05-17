"""End-to-end integration test for the deliberation system (V2).

V2: single round, no evaluators, pragmatico wins by convention,
perspectives injected directly as context.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from durin.deliberation.engine import DeliberationEngine
from durin.deliberation.generator import GeneratorConfig
from durin.deliberation.types import (
    ConvergenceReason,
    DeliberationContext,
    GeneratorRole,
    TriggerReason,
)
from durin.providers.base import LLMResponse


def _mock_provider() -> AsyncMock:
    provider = AsyncMock()
    responses = {
        GeneratorRole.PRAGMATICO: "Implementar OAuth2 directamente con la library existente.",
        GeneratorRole.EXPLORADOR: "Usar passkeys en vez de passwords, es más seguro y moderno.",
        GeneratorRole.CRITICO: "Empezar con auth básico detrás de feature flag, rollback inmediato.",
    }
    call_count = [0]
    roles = list(GeneratorRole)[:3]

    async def _chat(**kwargs):
        idx = call_count[0] % len(roles)
        call_count[0] += 1
        role = roles[idx]
        return LLMResponse(
            content=responses[role],
            tool_calls=[],
            finish_reason="stop",
            usage={"prompt_tokens": 80, "completion_tokens": 30},
        )

    provider.chat.side_effect = _chat
    return provider


def _generators() -> list[GeneratorConfig]:
    return [
        GeneratorConfig(
            role=GeneratorRole.PRAGMATICO,
            model="qwen2.5:7b",
            temperature=0.3,
            prompt_template="Proponé la acción más directa.",
        ),
        GeneratorConfig(
            role=GeneratorRole.EXPLORADOR,
            model="qwen2.5:7b",
            temperature=0.8,
            prompt_template="Proponé algo no obvio.",
        ),
        GeneratorConfig(
            role=GeneratorRole.CRITICO,
            model="qwen2.5:7b",
            temperature=0.5,
            prompt_template="Proponé la acción más segura.",
        ),
    ]


class TestFullDeliberationCycleV2:
    @pytest.mark.asyncio
    async def test_v2_pragmatico_wins_by_convention(self):
        """V2: pragmatico always wins (no evaluator scoring)."""
        engine = DeliberationEngine(
            provider=_mock_provider(),
            generators=_generators(),
            evaluators=[],
            max_rounds=1,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="implement user authentication",
            recent_context="user asked for login",
            posture_snapshot={"cautela": 0.5, "profundidad": 0.5},
        )
        verdict = await engine.deliberate(ctx)
        assert verdict.accepted is True
        assert verdict.rounds_used == 1
        assert verdict.winner.proposal.role == GeneratorRole.PRAGMATICO

    @pytest.mark.asyncio
    async def test_v2_single_round_only(self):
        """V2: always 1 round regardless of max_rounds setting."""
        engine = DeliberationEngine(
            provider=_mock_provider(),
            generators=_generators(),
            evaluators=[],
            max_rounds=5,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="complex task",
            recent_context="",
            posture_snapshot={"cautela": 0.5, "profundidad": 0.5},
        )
        verdict = await engine.deliberate(ctx)
        assert verdict.rounds_used == 1
        assert verdict.convergence_reason == ConvergenceReason.THRESHOLD

    @pytest.mark.asyncio
    async def test_v2_all_proposals_have_neutral_scores(self):
        """V2: all proposals get 0.5 score (no evaluators)."""
        engine = DeliberationEngine(
            provider=_mock_provider(),
            generators=_generators(),
            evaluators=[],
            max_rounds=1,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="test observability",
            recent_context="",
            posture_snapshot={"cautela": 0.5, "profundidad": 0.5},
        )
        verdict = await engine.deliberate(ctx)
        assert len(verdict.all_proposals) == 3
        for sp in verdict.all_proposals:
            assert sp.final_score == 0.5
            assert sp.scores == ()

    @pytest.mark.asyncio
    async def test_v2_no_under_doubt(self):
        """V2: never produces under_doubt (no scoring to fail threshold)."""
        engine = DeliberationEngine(
            provider=_mock_provider(),
            generators=_generators(),
            evaluators=[],
            max_rounds=1,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="ambiguous task",
            recent_context="",
            posture_snapshot={"cautela": 0.9, "profundidad": 0.9},
        )
        verdict = await engine.deliberate(ctx)
        assert verdict.under_doubt is False

    @pytest.mark.asyncio
    async def test_v2_posture_influences_generator_count(self):
        """High cautela can activate more generators via modulation."""
        engine = DeliberationEngine(
            provider=_mock_provider(),
            generators=_generators(),
            evaluators=[],
            max_rounds=1,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="deploy to production",
            recent_context="",
            posture_snapshot={"cautela": 0.9, "profundidad": 0.8},
        )
        verdict = await engine.deliberate(ctx)
        assert verdict.accepted is True
        assert len(verdict.all_proposals) >= 2
