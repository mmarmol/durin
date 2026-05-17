"""End-to-end integration test for the deliberation system."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from durin.deliberation.engine import DeliberationEngine
from durin.deliberation.evaluator import Evaluator
from durin.deliberation.generator import GeneratorConfig
from durin.deliberation.types import (
    DeliberationContext,
    EvaluationScore,
    GeneratorRole,
    Proposal,
    TriggerReason,
)
from durin.providers.base import LLMResponse


class _ScriptedEvaluator(Evaluator):
    """Returns pre-scripted scores keyed by role."""

    def __init__(self, name: str, scores_by_role: dict[GeneratorRole, float]):
        self._name = name
        self._scores_by_role = scores_by_role

    @property
    def name(self) -> str:
        return self._name

    async def evaluate(self, proposal: Proposal, context: DeliberationContext) -> EvaluationScore:
        score = self._scores_by_role.get(proposal.role, 0.5)
        return EvaluationScore(evaluator_name=self._name, score=score)


def _mock_provider() -> AsyncMock:
    provider = AsyncMock()
    responses = {
        GeneratorRole.PRAGMATICO: "Implementar OAuth2 directamente con la library existente.",
        GeneratorRole.EXPLORADOR: "Usar passkeys en vez de passwords, es más seguro y moderno.",
        GeneratorRole.CRITICO: "Empezar con auth básico detrás de feature flag, rollback inmediato.",
    }
    call_count = [0]
    roles = list(GeneratorRole)

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


class TestFullDeliberationCycle:
    @pytest.mark.asyncio
    async def test_default_cautela_selects_balanced_winner(self):
        """With neutral cautela (0.5), proposals scored equally in avance and reversibilidad."""
        evaluators = [
            _ScriptedEvaluator("avance", {
                GeneratorRole.PRAGMATICO: 0.7,
                GeneratorRole.EXPLORADOR: 0.5,
                GeneratorRole.CRITICO: 0.3,
            }),
            _ScriptedEvaluator("reversibilidad", {
                GeneratorRole.PRAGMATICO: 0.4,
                GeneratorRole.EXPLORADOR: 0.6,
                GeneratorRole.CRITICO: 0.9,
            }),
        ]
        engine = DeliberationEngine(
            provider=_mock_provider(),
            generators=_generators(),
            evaluators=evaluators,
            max_rounds=3,
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
        # All three scored, explorador has balanced 0.5+0.6=1.1 weighted equally
        # pragmatico: 0.5*0.7 + 0.5*0.4 = 0.55
        # explorador: 0.5*0.5 + 0.5*0.6 = 0.55
        # critico: 0.5*0.3 + 0.5*0.9 = 0.60 → winner
        assert verdict.winner.proposal.role == GeneratorRole.CRITICO

    @pytest.mark.asyncio
    async def test_high_cautela_favors_reversibilidad(self):
        """With high cautela (0.9), reversibilidad-heavy proposal wins."""
        evaluators = [
            _ScriptedEvaluator("avance", {
                GeneratorRole.PRAGMATICO: 0.9,
                GeneratorRole.EXPLORADOR: 0.5,
                GeneratorRole.CRITICO: 0.2,
            }),
            _ScriptedEvaluator("reversibilidad", {
                GeneratorRole.PRAGMATICO: 0.1,
                GeneratorRole.EXPLORADOR: 0.5,
                GeneratorRole.CRITICO: 0.95,
            }),
        ]
        engine = DeliberationEngine(
            provider=_mock_provider(),
            generators=_generators(),
            evaluators=evaluators,
            max_rounds=1,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.CRITICAL_ACTION,
            goal_summary="deploy to production",
            recent_context="",
            posture_snapshot={"cautela": 0.9, "profundidad": 0.5},
        )
        verdict = await engine.deliberate(ctx)
        assert verdict.winner.proposal.role == GeneratorRole.CRITICO

    @pytest.mark.asyncio
    async def test_low_cautela_favors_avance(self):
        """With low cautela (0.1), progress-heavy proposal wins."""
        evaluators = [
            _ScriptedEvaluator("avance", {
                GeneratorRole.PRAGMATICO: 0.9,
                GeneratorRole.EXPLORADOR: 0.5,
                GeneratorRole.CRITICO: 0.2,
            }),
            _ScriptedEvaluator("reversibilidad", {
                GeneratorRole.PRAGMATICO: 0.1,
                GeneratorRole.EXPLORADOR: 0.5,
                GeneratorRole.CRITICO: 0.95,
            }),
        ]
        engine = DeliberationEngine(
            provider=_mock_provider(),
            generators=_generators(),
            evaluators=evaluators,
            max_rounds=1,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="quick prototype",
            recent_context="",
            posture_snapshot={"cautela": 0.1, "profundidad": 0.3},
        )
        verdict = await engine.deliberate(ctx)
        assert verdict.winner.proposal.role == GeneratorRole.PRAGMATICO

    @pytest.mark.asyncio
    async def test_multi_round_retry(self):
        """Low scores in round 1 trigger round 2 with better outcomes."""
        round_scores = {}

        class _RoundAware(Evaluator):
            def __init__(self, name: str):
                self._name = name

            @property
            def name(self) -> str:
                return self._name

            async def evaluate(self, proposal, context):
                if proposal.round_number == 1:
                    return EvaluationScore(self._name, 0.3)
                return EvaluationScore(self._name, 0.8)

        engine = DeliberationEngine(
            provider=_mock_provider(),
            generators=_generators(),
            evaluators=[_RoundAware("avance"), _RoundAware("reversibilidad")],
            max_rounds=3,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="complex task",
            recent_context="",
            posture_snapshot={"cautela": 0.5, "profundidad": 0.5},
        )
        verdict = await engine.deliberate(ctx)
        assert verdict.accepted is True
        assert verdict.rounds_used == 2

    @pytest.mark.asyncio
    async def test_under_doubt_after_plateau(self):
        """All rounds produce same low scores → plateau convergence with under_doubt."""
        evaluators = [
            _ScriptedEvaluator("avance", {r: 0.3 for r in GeneratorRole}),
            _ScriptedEvaluator("reversibilidad", {r: 0.3 for r in GeneratorRole}),
        ]
        engine = DeliberationEngine(
            provider=_mock_provider(),
            generators=_generators(),
            evaluators=evaluators,
            max_rounds=3,
        )
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="ambiguous task",
            recent_context="",
            posture_snapshot={"cautela": 0.5, "profundidad": 0.5},
        )
        verdict = await engine.deliberate(ctx)
        assert verdict.accepted is True
        assert verdict.under_doubt is True
        # Flat scores → plateau detected in round 2
        assert verdict.rounds_used == 2

    @pytest.mark.asyncio
    async def test_verdict_contains_all_proposals(self):
        """Full verdict trace available for observability."""
        evaluators = [
            _ScriptedEvaluator("avance", {r: 0.7 for r in GeneratorRole}),
            _ScriptedEvaluator("reversibilidad", {r: 0.7 for r in GeneratorRole}),
        ]
        engine = DeliberationEngine(
            provider=_mock_provider(),
            generators=_generators(),
            evaluators=evaluators,
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
            assert sp.final_score == pytest.approx(0.7)
            assert len(sp.scores) == 2
