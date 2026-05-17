"""Tests for deliberation engine V2 — perspective generation without evaluators."""

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


class TestEngineV2Basics:
    @pytest.mark.asyncio
    async def test_generates_3_perspectives(self):
        provider = _mock_provider()
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        verdict = await engine.deliberate(_context())
        assert len(verdict.all_proposals) == 3
        roles = {sp.proposal.role for sp in verdict.all_proposals}
        assert roles == {GeneratorRole.PRAGMATICO, GeneratorRole.EXPLORADOR, GeneratorRole.CRITICO}

    @pytest.mark.asyncio
    async def test_single_round_always(self):
        provider = _mock_provider()
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        verdict = await engine.deliberate(_context())
        assert verdict.rounds_used == 1
        assert verdict.accepted is True

    @pytest.mark.asyncio
    async def test_no_evaluator_calls(self):
        provider = _mock_provider()
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        await engine.deliberate(_context())
        # Only 3 calls (one per generator), no evaluator calls
        assert provider.chat.call_count == 3

    @pytest.mark.asyncio
    async def test_pragmatico_is_winner_by_convention(self):
        provider = _mock_provider()
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        verdict = await engine.deliberate(_context())
        assert verdict.winner.proposal.role == GeneratorRole.PRAGMATICO

    @pytest.mark.asyncio
    async def test_all_proposals_have_neutral_score(self):
        provider = _mock_provider()
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        verdict = await engine.deliberate(_context())
        for sp in verdict.all_proposals:
            assert sp.final_score == 0.5
            assert sp.scores == ()

    @pytest.mark.asyncio
    async def test_convergence_reason_is_threshold(self):
        provider = _mock_provider()
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        verdict = await engine.deliberate(_context())
        assert verdict.convergence_reason == ConvergenceReason.THRESHOLD


class TestEngineV2ErrorHandling:
    @pytest.mark.asyncio
    async def test_partial_generator_failure(self):
        call_count = [0]

        async def _chat(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("model unavailable")
            return LLMResponse(content="proposal", tool_calls=[], finish_reason="stop", usage={})

        provider = AsyncMock()
        provider.chat.side_effect = _chat
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        verdict = await engine.deliberate(_context())
        assert verdict.accepted is True
        assert len(verdict.all_proposals) == 2

    @pytest.mark.asyncio
    async def test_all_generators_fail_returns_empty_verdict(self):
        provider = AsyncMock()
        provider.chat.side_effect = RuntimeError("all dead")
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        verdict = await engine.deliberate(_context())
        assert verdict.under_doubt is True
        assert verdict.winner.proposal.content == ""

    @pytest.mark.asyncio
    async def test_empty_content_handled(self):
        provider = _mock_provider(["", "valid response", ""])
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        verdict = await engine.deliberate(_context())
        assert len(verdict.all_proposals) == 3


class TestEngineV2PostureInfluence:
    @pytest.mark.asyncio
    async def test_high_cautela_adds_extra_generators(self):
        """With cautela > 0.85, modulator adds extra critico + pragmatico."""
        provider = _mock_provider(["a", "b", "c", "d", "e"])
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        verdict = await engine.deliberate(_context(cautela=0.95))
        # Should have 5 proposals: original 3 + extra pragmatico + extra critico
        assert len(verdict.all_proposals) == 5

    @pytest.mark.asyncio
    async def test_normal_cautela_includes_all_3(self):
        provider = _mock_provider()
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        verdict = await engine.deliberate(_context(cautela=0.5))
        assert len(verdict.all_proposals) == 3
        roles = {sp.proposal.role for sp in verdict.all_proposals}
        assert roles == {GeneratorRole.PRAGMATICO, GeneratorRole.EXPLORADOR, GeneratorRole.CRITICO}

    @pytest.mark.asyncio
    async def test_low_profundidad_excludes_critico(self):
        """With profundidad < 0.3, critico is omitted."""
        provider = _mock_provider(["fast", "creative"])
        engine = DeliberationEngine(
            provider=provider, generators=_generators(),
            evaluators=[], max_rounds=1,
        )
        verdict = await engine.deliberate(_context(profundidad=0.2))
        roles = {sp.proposal.role for sp in verdict.all_proposals}
        assert GeneratorRole.CRITICO not in roles
        assert len(verdict.all_proposals) == 2
