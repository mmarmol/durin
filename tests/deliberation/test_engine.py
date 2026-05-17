"""Tests for deliberation engine."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from durin.deliberation.engine import DeliberationEngine, _extra_rounds, _PLATEAU_THRESHOLD
from durin.deliberation.evaluator import Evaluator
from durin.deliberation.generator import GeneratorConfig
from durin.deliberation.types import (
    ConvergenceReason,
    DeliberationContext,
    EvaluationScore,
    GeneratorRole,
    Proposal,
    TriggerReason,
)
from durin.providers.base import LLMResponse


def _context(cautela: float = 0.5, profundidad: float = 0.5) -> DeliberationContext:
    return DeliberationContext(
        trigger=TriggerReason.PLANNING_MOMENT,
        goal_summary="implement feature",
        recent_context="",
        posture_snapshot={"cautela": cautela, "profundidad": profundidad},
    )


def _generators() -> list[GeneratorConfig]:
    return [
        GeneratorConfig(role=GeneratorRole.PRAGMATICO, model="test"),
        GeneratorConfig(role=GeneratorRole.EXPLORADOR, model="test"),
        GeneratorConfig(role=GeneratorRole.CRITICO, model="test"),
    ]


class _FixedEvaluator(Evaluator):
    def __init__(self, name: str, scores: dict[str, float]):
        self._name = name
        self._scores = scores

    @property
    def name(self) -> str:
        return self._name

    async def evaluate(self, proposal: Proposal, context: DeliberationContext) -> EvaluationScore:
        key = f"{proposal.role}:{proposal.round_number}"
        score = self._scores.get(key, 0.5)
        return EvaluationScore(evaluator_name=self._name, score=score)


class _FailingEvaluator(Evaluator):
    @property
    def name(self) -> str:
        return "failing"

    async def evaluate(self, proposal: Proposal, context: DeliberationContext) -> EvaluationScore:
        raise RuntimeError("evaluator crash")


def _mock_provider(responses: list[str] | None = None) -> AsyncMock:
    provider = AsyncMock()
    if responses is None:
        responses = ["do X directly", "try Y creatively", "avoid Z for safety"]

    call_count = 0

    async def _chat(**kwargs):
        nonlocal call_count
        content = responses[call_count % len(responses)]
        call_count += 1
        return LLMResponse(content=content, tool_calls=[], finish_reason="stop", usage={})

    provider.chat.side_effect = _chat
    return provider


class TestEngineBasics:
    @pytest.mark.asyncio
    async def test_single_round_accepted(self):
        provider = _mock_provider()
        evaluators = [
            _FixedEvaluator("avance", {"pragmatico:1": 0.8, "explorador:1": 0.6, "critico:1": 0.4}),
            _FixedEvaluator("reversibilidad", {"pragmatico:1": 0.7, "explorador:1": 0.7, "critico:1": 0.9}),
        ]
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=evaluators, max_rounds=3,
        )
        verdict = await engine.deliberate(_context())
        assert verdict.accepted is True
        assert verdict.rounds_used == 1
        assert len(verdict.all_proposals) == 3

    @pytest.mark.asyncio
    async def test_low_scores_trigger_retry(self):
        provider = _mock_provider()
        call_count = [0]

        class _RoundAwareEvaluator(Evaluator):
            def __init__(self, name: str):
                self._name = name

            @property
            def name(self) -> str:
                return self._name

            async def evaluate(self, proposal, context):
                call_count[0] += 1
                # Low scores in round 1, high in round 2
                score = 0.2 if proposal.round_number == 1 else 0.9
                return EvaluationScore(evaluator_name=self._name, score=score)

        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[_RoundAwareEvaluator("avance"), _RoundAwareEvaluator("reversibilidad")],
            max_rounds=3,
        )
        verdict = await engine.deliberate(_context())
        assert verdict.accepted is True
        assert verdict.rounds_used == 2

    @pytest.mark.asyncio
    async def test_max_rounds_forces_acceptance(self):
        provider = _mock_provider()

        class _SlowlyImprovingEvaluator(Evaluator):
            """Scores improve per round but never reach threshold."""
            def __init__(self, name: str):
                self._name = name

            @property
            def name(self) -> str:
                return self._name

            async def evaluate(self, proposal, context):
                base = 0.25 + proposal.round_number * 0.06
                return EvaluationScore(evaluator_name=self._name, score=min(base, 0.6))

        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[_SlowlyImprovingEvaluator("avance"), _SlowlyImprovingEvaluator("reversibilidad")],
            max_rounds=3,
        )
        # profundidad=1.0 → threshold=0.7, scores never reach 0.7
        # profundidad=1.0 also grants +2 extra rounds → effective_max=5
        # Scores improve 0.06/round → above plateau threshold of 0.05
        verdict = await engine.deliberate(_context(profundidad=1.0))
        assert verdict.accepted is True
        assert verdict.under_doubt is True
        assert verdict.rounds_used == 5
        assert verdict.convergence_reason == ConvergenceReason.MAX_ROUNDS


class TestEngineErrorHandling:
    @pytest.mark.asyncio
    async def test_generator_failure_continues(self):
        provider = AsyncMock()
        call_count = [0]

        async def _chat(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("model unavailable")
            return LLMResponse(content="proposal", tool_calls=[], finish_reason="stop", usage={})

        provider.chat.side_effect = _chat
        evaluators = [
            _FixedEvaluator("avance", {"explorador:1": 0.9, "critico:1": 0.8}),
            _FixedEvaluator("reversibilidad", {"explorador:1": 0.9, "critico:1": 0.8}),
        ]
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=evaluators, max_rounds=1,
        )
        verdict = await engine.deliberate(_context())
        assert verdict.accepted is True
        assert len(verdict.all_proposals) == 2

    @pytest.mark.asyncio
    async def test_evaluator_failure_uses_fallback(self):
        provider = _mock_provider()
        evaluators = [
            _FixedEvaluator("avance", {"pragmatico:1": 0.9, "explorador:1": 0.9, "critico:1": 0.9}),
            _FailingEvaluator(),
        ]
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=evaluators, max_rounds=3,
        )
        verdict = await engine.deliberate(_context())
        assert verdict.accepted is True

    @pytest.mark.asyncio
    async def test_all_generators_fail_returns_empty_verdict(self):
        provider = AsyncMock()
        provider.chat.side_effect = RuntimeError("all dead")
        evaluators = [_FixedEvaluator("avance", {})]
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=evaluators, max_rounds=3,
        )
        verdict = await engine.deliberate(_context())
        assert verdict.under_doubt is True
        assert verdict.winner.proposal.content == ""


class TestExtraRounds:
    def test_profundidad_high_gives_2_extra(self):
        assert _extra_rounds(0.9) == 2
        assert _extra_rounds(0.8) == 2

    def test_profundidad_medium_high_gives_1_extra(self):
        assert _extra_rounds(0.7) == 1
        assert _extra_rounds(0.6) == 1

    def test_profundidad_normal_gives_0_extra(self):
        assert _extra_rounds(0.5) == 0
        assert _extra_rounds(0.3) == 0
        assert _extra_rounds(0.0) == 0

    @pytest.mark.asyncio
    async def test_effective_max_rounds_capped_at_5(self):
        provider = _mock_provider()

        class _SlowImprovingEval(Evaluator):
            def __init__(self, name: str):
                self._name = name

            @property
            def name(self) -> str:
                return self._name

            async def evaluate(self, proposal, context):
                base = 0.2 + proposal.round_number * 0.06
                return EvaluationScore(evaluator_name=self._name, score=min(base, 0.55))

        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[_SlowImprovingEval("avance"), _SlowImprovingEval("reversibilidad")],
            max_rounds=4,
        )
        # profundidad=0.9 → +2, but capped at 5 (not 6)
        # threshold = 0.4 + 0.3*0.9 = 0.67, scores cap at 0.55 < 0.67
        verdict = await engine.deliberate(_context(profundidad=0.9))
        assert verdict.rounds_used == 5

    @pytest.mark.asyncio
    async def test_normal_profundidad_no_extra_rounds(self):
        provider = _mock_provider()

        class _SlowImprovingEval(Evaluator):
            def __init__(self, name: str):
                self._name = name

            @property
            def name(self) -> str:
                return self._name

            async def evaluate(self, proposal, context):
                base = 0.3 + proposal.round_number * 0.06
                return EvaluationScore(evaluator_name=self._name, score=min(base, 0.5))

        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[_SlowImprovingEval("avance"), _SlowImprovingEval("reversibilidad")],
            max_rounds=3,
        )
        # profundidad=0.5 → threshold=0.55, scores never reach 0.55, no extra rounds
        verdict = await engine.deliberate(_context(profundidad=0.5))
        assert verdict.rounds_used == 3


class TestConvergence:
    @pytest.mark.asyncio
    async def test_convergence_by_threshold(self):
        provider = _mock_provider()
        evaluators = [
            _FixedEvaluator("avance", {"pragmatico:1": 0.9, "explorador:1": 0.5, "critico:1": 0.4}),
            _FixedEvaluator("reversibilidad", {"pragmatico:1": 0.8, "explorador:1": 0.5, "critico:1": 0.4}),
        ]
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=evaluators, max_rounds=3,
        )
        verdict = await engine.deliberate(_context())
        assert verdict.convergence_reason == ConvergenceReason.THRESHOLD
        assert verdict.rounds_used == 1

    @pytest.mark.asyncio
    async def test_convergence_by_plateau(self):
        provider = _mock_provider()

        class _FlatEval(Evaluator):
            def __init__(self, name: str):
                self._name = name

            @property
            def name(self) -> str:
                return self._name

            async def evaluate(self, proposal, context):
                return EvaluationScore(evaluator_name=self._name, score=0.4)

        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[_FlatEval("avance"), _FlatEval("reversibilidad")],
            max_rounds=5,
        )
        # All scores = 0.4 → no improvement → plateau in round 2
        verdict = await engine.deliberate(_context(profundidad=0.5))
        assert verdict.convergence_reason == ConvergenceReason.PLATEAU
        assert verdict.rounds_used == 2
        assert verdict.accepted is True


class TestEvolution:
    @pytest.mark.asyncio
    async def test_round2_generator_receives_evolution_context(self):
        """Round 2 generators receive the previous round's winning proposal."""
        call_messages = []
        call_count = [0]

        async def _capture_chat(**kwargs):
            call_count[0] += 1
            call_messages.append(kwargs.get("messages", []))
            return LLMResponse(content="proposal", tool_calls=[], finish_reason="stop", usage={})

        provider = AsyncMock()
        provider.chat.side_effect = _capture_chat

        class _RoundAwareEval(Evaluator):
            def __init__(self, name: str):
                self._name = name

            @property
            def name(self) -> str:
                return self._name

            async def evaluate(self, proposal, context):
                score = 0.3 if proposal.round_number == 1 else 0.9
                return EvaluationScore(evaluator_name=self._name, score=score)

        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[_RoundAwareEval("avance"), _RoundAwareEval("reversibilidad")],
            max_rounds=3,
        )
        await engine.deliberate(_context())

        # Round 2 generators should have evolution context in their user message
        round2_messages = call_messages[3:6]  # calls 4-6 are round 2 generators
        for msgs in round2_messages:
            user_msg = msgs[1]["content"]
            assert "ganadora" in user_msg.lower() or "Propuesta ganadora" in user_msg


class TestCrossover:
    @pytest.mark.asyncio
    async def test_crossover_fires_when_gap_small(self):
        """When top 2 proposals have gap < 0.10, a hybrid is generated."""
        provider = _mock_provider(["proposal A", "proposal B", "proposal C", "hybrid"])

        class _CloseScoreEval(Evaluator):
            def __init__(self, name: str):
                self._name = name
                self._call_count = 0

            @property
            def name(self) -> str:
                return self._name

            async def evaluate(self, proposal, context):
                self._call_count += 1
                if proposal.role == GeneratorRole.HIBRIDO:
                    return EvaluationScore(evaluator_name=self._name, score=0.85)
                if proposal.round_number == 1:
                    return EvaluationScore(evaluator_name=self._name, score=0.3)
                # Round 2: close scores to trigger crossover
                scores = {"pragmatico": 0.7, "explorador": 0.68, "critico": 0.5}
                return EvaluationScore(
                    evaluator_name=self._name,
                    score=scores.get(str(proposal.role), 0.5),
                )

        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[_CloseScoreEval("avance"), _CloseScoreEval("reversibilidad")],
            max_rounds=3,
        )
        verdict = await engine.deliberate(_context())
        # Hybrid should exist in all_proposals
        roles = [sp.proposal.role for sp in verdict.all_proposals]
        assert GeneratorRole.HIBRIDO in roles

    @pytest.mark.asyncio
    async def test_no_crossover_in_round1(self):
        """Crossover only fires in round 2+."""
        provider = _mock_provider()
        evaluators = [
            _FixedEvaluator("avance", {"pragmatico:1": 0.7, "explorador:1": 0.69, "critico:1": 0.4}),
            _FixedEvaluator("reversibilidad", {"pragmatico:1": 0.7, "explorador:1": 0.69, "critico:1": 0.4}),
        ]
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=evaluators, max_rounds=1,
        )
        # Gap between pragmatico (0.7) and explorador (0.69) = 0.01 < 0.10
        # But it's round 1 → no crossover
        verdict = await engine.deliberate(_context())
        roles = [sp.proposal.role for sp in verdict.all_proposals]
        assert GeneratorRole.HIBRIDO not in roles

    @pytest.mark.asyncio
    async def test_no_crossover_when_gap_large(self):
        """Gap >= 0.10 means no crossover."""
        provider = _mock_provider()

        class _RoundAwareEval(Evaluator):
            def __init__(self, name: str):
                self._name = name

            @property
            def name(self) -> str:
                return self._name

            async def evaluate(self, proposal, context):
                if proposal.round_number == 1:
                    return EvaluationScore(evaluator_name=self._name, score=0.3)
                # Round 2: clear gap
                scores = {"pragmatico": 0.8, "explorador": 0.5, "critico": 0.4}
                return EvaluationScore(
                    evaluator_name=self._name,
                    score=scores.get(str(proposal.role), 0.5),
                )

        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[_RoundAwareEval("avance"), _RoundAwareEval("reversibilidad")],
            max_rounds=3,
        )
        verdict = await engine.deliberate(_context())
        roles = [sp.proposal.role for sp in verdict.all_proposals]
        assert GeneratorRole.HIBRIDO not in roles


class TestEnginePostureInfluence:
    @pytest.mark.asyncio
    async def test_high_cautela_selects_safe_proposal(self):
        provider = _mock_provider(["attack fast", "try something", "be careful"])
        evaluators = [
            _FixedEvaluator("avance", {"pragmatico:1": 0.9, "explorador:1": 0.5, "critico:1": 0.2}),
            _FixedEvaluator("reversibilidad", {"pragmatico:1": 0.1, "explorador:1": 0.5, "critico:1": 0.9}),
        ]
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=evaluators, max_rounds=1,
        )
        verdict = await engine.deliberate(_context(cautela=0.95))
        # With cautela=0.95, reversibilidad weight dominates → critico should win
        assert verdict.winner.proposal.role == GeneratorRole.CRITICO

    @pytest.mark.asyncio
    async def test_low_cautela_selects_progress_proposal(self):
        provider = _mock_provider(["attack fast", "try something", "be careful"])
        evaluators = [
            _FixedEvaluator("avance", {"pragmatico:1": 0.9, "explorador:1": 0.5, "critico:1": 0.2}),
            _FixedEvaluator("reversibilidad", {"pragmatico:1": 0.1, "explorador:1": 0.5, "critico:1": 0.9}),
        ]
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=evaluators, max_rounds=1,
        )
        verdict = await engine.deliberate(_context(cautela=0.05))
        assert verdict.winner.proposal.role == GeneratorRole.PRAGMATICO
