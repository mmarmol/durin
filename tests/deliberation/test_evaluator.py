"""Tests for evaluator module."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from durin.deliberation.evaluator import LLMEvaluator, parse_score
from durin.deliberation.types import (
    DeliberationContext,
    GeneratorRole,
    Proposal,
    TriggerReason,
)
from durin.providers.base import LLMResponse


def _mock_provider(content: str) -> AsyncMock:
    provider = AsyncMock()
    provider.chat.return_value = LLMResponse(
        content=content, tool_calls=[], finish_reason="stop", usage={},
    )
    return provider


def _proposal() -> Proposal:
    return Proposal(role=GeneratorRole.PRAGMATICO, content="do X", round_number=1)


def _context() -> DeliberationContext:
    return DeliberationContext(
        trigger=TriggerReason.PLANNING_MOMENT,
        goal_summary="implement auth",
        recent_context="",
    )


class TestParseScore:
    def test_valid_float(self):
        score, rationale = parse_score("0.75")
        assert score == pytest.approx(0.75)
        assert rationale == ""

    def test_float_with_rationale(self):
        score, rationale = parse_score("0.8\ngood progress toward goal")
        assert score == pytest.approx(0.8)
        assert rationale == "good progress toward goal"

    def test_float_with_inline_text(self):
        score, rationale = parse_score("0.6 — moderately reversible")
        assert score == pytest.approx(0.6)
        assert "moderately reversible" in rationale

    def test_integer_one(self):
        score, _ = parse_score("1.0")
        assert score == pytest.approx(1.0)

    def test_integer_zero(self):
        score, _ = parse_score("0.0")
        assert score == pytest.approx(0.0)

    def test_bare_zero(self):
        score, _ = parse_score("0")
        assert score == pytest.approx(0.0)

    def test_bare_one(self):
        score, _ = parse_score("1")
        assert score == pytest.approx(1.0)

    def test_scale_0_10_normalized(self):
        score, _ = parse_score("7")
        assert score == pytest.approx(0.7)

    def test_scale_0_10_with_text(self):
        score, _ = parse_score("8 de 10")
        assert score == pytest.approx(0.8)

    def test_malformed_text(self):
        score, rationale = parse_score("high")
        assert score == pytest.approx(0.5)
        assert rationale == "high"

    def test_empty_string(self):
        score, rationale = parse_score("")
        assert score == pytest.approx(0.5)
        assert rationale == ""

    def test_whitespace_only(self):
        score, rationale = parse_score("   ")
        assert score == pytest.approx(0.5)
        assert rationale == ""

    def test_clamped_above_one(self):
        # regex won't match >1 naturally, but test the clamp logic
        score, _ = parse_score("1.0 perfect")
        assert score <= 1.0


class TestLLMEvaluator:
    @pytest.mark.asyncio
    async def test_returns_score(self):
        provider = _mock_provider("0.75")
        evaluator = LLMEvaluator(
            _name="avance", _provider=provider, _model="qwen2.5:7b",
            _prompt_template="Score it.",
        )
        result = await evaluator.evaluate(_proposal(), _context())
        assert result.evaluator_name == "avance"
        assert result.score == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_name_property(self):
        provider = _mock_provider("0.5")
        evaluator = LLMEvaluator(
            _name="reversibilidad", _provider=provider, _model="x",
            _prompt_template="",
        )
        assert evaluator.name == "reversibilidad"

    @pytest.mark.asyncio
    async def test_passes_proposal_in_user_message(self):
        provider = _mock_provider("0.5")
        evaluator = LLMEvaluator(
            _name="avance", _provider=provider, _model="x",
            _prompt_template="Score it.",
        )
        await evaluator.evaluate(_proposal(), _context())
        messages = provider.chat.call_args[1]["messages"]
        user_msg = messages[1]["content"]
        assert "do X" in user_msg
        assert "implement auth" in user_msg

    @pytest.mark.asyncio
    async def test_uses_zero_temperature(self):
        provider = _mock_provider("0.5")
        evaluator = LLMEvaluator(
            _name="avance", _provider=provider, _model="x",
            _prompt_template="", _temperature=0.0,
        )
        await evaluator.evaluate(_proposal(), _context())
        assert provider.chat.call_args[1]["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_malformed_response_fallback(self):
        provider = _mock_provider("I think it's good")
        evaluator = LLMEvaluator(
            _name="avance", _provider=provider, _model="x",
            _prompt_template="",
        )
        result = await evaluator.evaluate(_proposal(), _context())
        assert result.score == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_none_response_fallback(self):
        provider = AsyncMock()
        provider.chat.return_value = LLMResponse(
            content=None, tool_calls=[], finish_reason="stop", usage={},
        )
        evaluator = LLMEvaluator(
            _name="avance", _provider=provider, _model="x",
            _prompt_template="",
        )
        result = await evaluator.evaluate(_proposal(), _context())
        assert result.score == pytest.approx(0.5)
