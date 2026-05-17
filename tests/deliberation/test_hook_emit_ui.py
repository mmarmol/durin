"""Tests for DeliberationHook emit_ui integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from durin.agent.hook import AgentHookContext
from durin.deliberation.engine import DeliberationEngine
from durin.deliberation.evaluator import LLMEvaluator
from durin.deliberation.generator import GeneratorConfig
from durin.deliberation.hook import DeliberationHook
from durin.deliberation.types import GeneratorRole, TriggerReason
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
        GeneratorConfig(
            role=GeneratorRole.PRAGMATICO, model="m", temperature=0.3,
            prompt_template="test",
        ),
        GeneratorConfig(
            role=GeneratorRole.EXPLORADOR, model="m", temperature=0.8,
            prompt_template="test",
        ),
        GeneratorConfig(
            role=GeneratorRole.CRITICO, model="m", temperature=0.5,
            prompt_template="test",
        ),
    ]
    evaluators = [
        LLMEvaluator("avance", provider, "m", "score"),
        LLMEvaluator("reversibilidad", provider, "m", "score"),
    ]
    return DeliberationEngine(
        provider=provider, generators=generators,
        evaluators=evaluators, max_rounds=1,
    )


def _make_context(iteration: int = 0, emit_ui=None) -> AgentHookContext:
    return AgentHookContext(
        iteration=iteration,
        messages=[
            {"role": "system", "content": "You are an assistant."},
            {"role": "user", "content": "implementar login"},
        ],
        emit_ui=emit_ui,
    )


class TestDeliberationHookEmitUI:
    @pytest.mark.asyncio
    async def test_emits_deliberation_result_on_planning(self):
        responses = [
            "usar JWT estándar", "explorar passkeys", "OAuth2 es más seguro",
            "7", "8", "6", "5", "7", "9",
        ]
        provider = _mock_provider(responses)
        engine = _make_engine(provider)
        emit = AsyncMock()

        hook = DeliberationHook(engine=engine)
        ctx = _make_context(iteration=0, emit_ui=emit)

        await hook.before_iteration(ctx)

        emit.assert_called_once()
        kind, data = emit.call_args[0]
        assert kind == "deliberation_result"
        assert data["accepted"] is True
        assert data["winner"] is not None
        assert data["winner"]["role"] in ("pragmatico", "explorador", "critico")
        assert len(data["proposals"]) == 3
        assert "threshold" in data
        assert "rounds_used" in data

    @pytest.mark.asyncio
    async def test_no_emit_when_no_callback(self):
        responses = [
            "usar JWT estándar", "explorar passkeys", "OAuth2 es más seguro",
            "7", "8", "6", "5", "7", "9",
        ]
        provider = _mock_provider(responses)
        engine = _make_engine(provider)

        hook = DeliberationHook(engine=engine)
        ctx = _make_context(iteration=0, emit_ui=None)

        await hook.before_iteration(ctx)
        assert hook.last_verdict is not None

    @pytest.mark.asyncio
    async def test_does_not_emit_on_non_first_iteration(self):
        responses = ["x"] * 20
        provider = _mock_provider(responses)
        engine = _make_engine(provider)
        emit = AsyncMock()

        hook = DeliberationHook(engine=engine)
        ctx = _make_context(iteration=1, emit_ui=emit)

        await hook.before_iteration(ctx)

        emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_verdict_to_ui_structure(self):
        responses = [
            "usar JWT estándar", "explorar passkeys", "OAuth2 es más seguro",
            "7", "8", "6", "5", "7", "9",
        ]
        provider = _mock_provider(responses)
        engine = _make_engine(provider)
        emit = AsyncMock()

        hook = DeliberationHook(engine=engine)
        ctx = _make_context(iteration=0, emit_ui=emit)

        await hook.before_iteration(ctx)

        _, data = emit.call_args[0]
        assert isinstance(data["threshold"], float)
        assert isinstance(data["rounds_used"], int)
        assert isinstance(data["under_doubt"], bool)
        for p in data["proposals"]:
            assert "role" in p
            assert "content" in p
            assert "score" in p
