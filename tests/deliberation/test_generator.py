"""Tests for proposal generator."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from durin.deliberation.generator import (
    GeneratorConfig,
    _build_system_prompt_seed,
    _build_system_prompt_evolve,
    _build_user_prompt,
    _build_user_prompt_evolve,
    generate_proposal,
)
from durin.deliberation.types import (
    DeliberationContext,
    EvaluationScore,
    GeneratorRole,
    Proposal,
    RoundResult,
    ScoredProposal,
    TriggerReason,
)
from durin.providers.base import LLMResponse


def _mock_provider(content: str = "do the thing", usage: dict | None = None) -> AsyncMock:
    provider = AsyncMock()
    provider.chat.return_value = LLMResponse(
        content=content,
        tool_calls=[],
        finish_reason="stop",
        usage=usage or {"prompt_tokens": 50, "completion_tokens": 20},
    )
    return provider


def _context() -> DeliberationContext:
    return DeliberationContext(
        trigger=TriggerReason.PLANNING_MOMENT,
        goal_summary="implement user auth",
        recent_context="user asked for login page",
        posture_snapshot={"cautela": 0.6},
    )


def _config(role: GeneratorRole = GeneratorRole.PRAGMATICO) -> GeneratorConfig:
    return GeneratorConfig(
        role=role,
        model="qwen2.5:7b",
        temperature=0.7,
        max_tokens=512,
        prompt_template="Proponé la acción más directa.",
    )


class TestBuildSystemPromptSeed:
    def test_includes_template(self):
        config = _config()
        prompt = _build_system_prompt_seed(config, "")
        assert "Proponé la acción más directa" in prompt

    def test_includes_posture_phrase(self):
        config = _config()
        prompt = _build_system_prompt_seed(config, "Priorizá reversibilidad.")
        assert "Priorizá reversibilidad" in prompt

    def test_includes_response_instruction(self):
        config = _config()
        prompt = _build_system_prompt_seed(config, "")
        assert "1-3 oraciones" in prompt

    def test_empty_template_still_works(self):
        config = GeneratorConfig(role=GeneratorRole.EXPLORADOR, model="x")
        prompt = _build_system_prompt_seed(config, "test")
        assert "test" in prompt


class TestBuildUserPrompt:
    def test_includes_goal(self):
        ctx = _context()
        prompt = _build_user_prompt(ctx)
        assert "implement user auth" in prompt

    def test_includes_recent_context(self):
        ctx = _context()
        prompt = _build_user_prompt(ctx)
        assert "login page" in prompt

    def test_empty_context_omitted(self):
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="deploy",
            recent_context="",
        )
        prompt = _build_user_prompt(ctx)
        assert "Contexto reciente" not in prompt


class TestGenerateProposal:
    @pytest.mark.asyncio
    async def test_returns_proposal_with_content(self):
        provider = _mock_provider("use OAuth2 with JWT tokens")
        proposal = await generate_proposal(
            provider, _config(), _context(), round_number=1,
        )
        assert proposal.content == "use OAuth2 with JWT tokens"
        assert proposal.role == GeneratorRole.PRAGMATICO
        assert proposal.round_number == 1

    @pytest.mark.asyncio
    async def test_passes_correct_model(self):
        provider = _mock_provider()
        await generate_proposal(provider, _config(), _context(), round_number=1)
        call_kwargs = provider.chat.call_args[1]
        assert call_kwargs["model"] == "qwen2.5:7b"

    @pytest.mark.asyncio
    async def test_passes_temperature(self):
        provider = _mock_provider()
        config = GeneratorConfig(
            role=GeneratorRole.EXPLORADOR, model="x", temperature=1.0,
        )
        await generate_proposal(provider, config, _context(), round_number=1)
        call_kwargs = provider.chat.call_args[1]
        assert call_kwargs["temperature"] == 1.0

    @pytest.mark.asyncio
    async def test_no_tools_passed(self):
        provider = _mock_provider()
        await generate_proposal(provider, _config(), _context(), round_number=1)
        call_kwargs = provider.chat.call_args[1]
        assert call_kwargs["tools"] is None

    @pytest.mark.asyncio
    async def test_empty_response_graceful(self):
        provider = _mock_provider(content="")
        proposal = await generate_proposal(
            provider, _config(), _context(), round_number=1,
        )
        assert proposal.content == ""

    @pytest.mark.asyncio
    async def test_none_response_graceful(self):
        provider = AsyncMock()
        provider.chat.return_value = LLMResponse(
            content=None, tool_calls=[], finish_reason="stop", usage={},
        )
        proposal = await generate_proposal(
            provider, _config(), _context(), round_number=1,
        )
        assert proposal.content == ""

    @pytest.mark.asyncio
    async def test_metadata_includes_usage(self):
        provider = _mock_provider(usage={"prompt_tokens": 100, "completion_tokens": 30})
        proposal = await generate_proposal(
            provider, _config(), _context(), round_number=1,
        )
        assert proposal.metadata["usage"]["prompt_tokens"] == 100

    @pytest.mark.asyncio
    async def test_posture_phrase_in_system_message(self):
        provider = _mock_provider()
        await generate_proposal(
            provider, _config(), _context(), round_number=1,
            posture_phrase="Sé directo.",
        )
        messages = provider.chat.call_args[1]["messages"]
        system = messages[0]["content"]
        assert "Sé directo" in system


def _make_round_result() -> RoundResult:
    winner_proposal = Proposal(role=GeneratorRole.EXPLORADOR, content="use shadow traffic", round_number=1)
    own_proposal = Proposal(role=GeneratorRole.PRAGMATICO, content="deploy direct", round_number=1)
    winner = ScoredProposal(proposal=winner_proposal, scores=(EvaluationScore(evaluator_name="avance", score=0.8),), final_score=0.75)
    own = ScoredProposal(proposal=own_proposal, scores=(EvaluationScore(evaluator_name="avance", score=0.6),), final_score=0.55)
    return RoundResult(proposals=(winner, own), winner=winner, round_number=1)


class TestEvolutionContext:
    @pytest.mark.asyncio
    async def test_round2_uses_evolution_prompt(self):
        provider = _mock_provider("refined approach")
        evolution = _make_round_result()
        proposal = await generate_proposal(
            provider, _config(), _context(), round_number=2,
            evolution_context=evolution,
        )
        messages = provider.chat.call_args[1]["messages"]
        user_msg = messages[1]["content"]
        assert "use shadow traffic" in user_msg
        assert "ganadora" in user_msg.lower()
        assert proposal.round_number == 2

    @pytest.mark.asyncio
    async def test_round2_system_has_evolve_instruction(self):
        provider = _mock_provider("evolved")
        evolution = _make_round_result()
        await generate_proposal(
            provider, _config(), _context(), round_number=2,
            evolution_context=evolution,
        )
        messages = provider.chat.call_args[1]["messages"]
        system_msg = messages[0]["content"]
        assert "Refiná" in system_msg

    @pytest.mark.asyncio
    async def test_round1_ignores_evolution_context(self):
        provider = _mock_provider("seed")
        evolution = _make_round_result()
        await generate_proposal(
            provider, _config(), _context(), round_number=1,
            evolution_context=evolution,
        )
        messages = provider.chat.call_args[1]["messages"]
        user_msg = messages[1]["content"]
        assert "ganadora" not in user_msg.lower()

    def test_build_user_prompt_evolve_includes_winner(self):
        evolution = _make_round_result()
        prompt = _build_user_prompt_evolve(_context(), GeneratorRole.PRAGMATICO, evolution)
        assert "use shadow traffic" in prompt
        assert "75%" in prompt

    def test_build_user_prompt_evolve_includes_own(self):
        evolution = _make_round_result()
        prompt = _build_user_prompt_evolve(_context(), GeneratorRole.PRAGMATICO, evolution)
        assert "deploy direct" in prompt
        assert "55%" in prompt

    def test_build_user_prompt_evolve_no_own_for_missing_role(self):
        evolution = _make_round_result()
        prompt = _build_user_prompt_evolve(_context(), GeneratorRole.CRITICO, evolution)
        assert "Tu propuesta anterior" not in prompt

    def test_build_system_prompt_evolve_has_instruction(self):
        config = _config()
        evolution = _make_round_result()
        prompt = _build_system_prompt_evolve(config, "postura", evolution)
        assert "Refiná" in prompt
        assert "postura" in prompt
