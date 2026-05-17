"""Tests for deliberation engine V3 — single-call multi-perspective."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from durin.deliberation.engine import DeliberationEngine, _extract_sections
from durin.deliberation.types import DeliberationContext, DeliberationResult
from durin.providers.base import LLMResponse


def _context(**posture) -> DeliberationContext:
    return DeliberationContext(
        goal_summary="Fix numpy view assignment bug",
        investigation_context="output_field is a numpy structured array view",
        posture_snapshot=posture,
    )


def _mock_provider(content: str) -> AsyncMock:
    provider = AsyncMock()
    provider.chat.return_value = LLMResponse(content=content)
    return provider


_WELL_FORMED = """\
[CRITICO]
output_field = x rebinds the local variable, doesn't write to the array.

[EXPLORADOR]
Use np.copyto() for explicit memory writes without rebinding.

[PRAGMATICO]
Use output_field[...] = x to write through the view.

[SINTESIS]
The fix must use in-place assignment via [...] or copyto.
"""


class TestParseResponse:
    def test_parses_all_sections(self):
        result = DeliberationEngine._parse_response(_WELL_FORMED)
        assert len(result.perspectives) == 3
        assert result.perspectives[0].role == "critico"
        assert result.perspectives[1].role == "explorador"
        assert result.perspectives[2].role == "pragmatico"
        assert "in-place" in result.synthesis

    def test_fallback_on_unstructured(self):
        result = DeliberationEngine._parse_response("Just do the obvious thing.")
        assert len(result.perspectives) == 1
        assert result.perspectives[0].role == "pragmatico"
        assert result.synthesis == "Just do the obvious thing."

    def test_missing_synthesis_uses_last_perspective(self):
        text = "[CRITICO]\nRisk A.\n\n[EXPLORADOR]\nAlt B.\n\n[PRAGMATICO]\nDo C."
        result = DeliberationEngine._parse_response(text)
        assert len(result.perspectives) == 3
        assert result.synthesis == "Do C."

    def test_partial_sections(self):
        text = "[CRITICO]\nOnly risk here.\n\n[SINTESIS]\nJust this."
        result = DeliberationEngine._parse_response(text)
        assert len(result.perspectives) == 1
        assert result.perspectives[0].role == "critico"
        assert result.synthesis == "Just this."


class TestExtractSections:
    def test_basic(self):
        sections = _extract_sections("[CRITICO]\nA\n[EXPLORADOR]\nB")
        assert sections["critico"] == "A"
        assert sections["explorador"] == "B"

    def test_case_insensitive(self):
        sections = _extract_sections("[critico]\ntest")
        assert "critico" in sections

    def test_empty_text(self):
        assert _extract_sections("") == {}

    def test_no_markers(self):
        assert _extract_sections("plain text without markers") == {}


class TestDeliberate:
    @pytest.fixture
    def engine(self):
        provider = _mock_provider(_WELL_FORMED)
        return DeliberationEngine(
            provider=provider,
            model="glm-5.1",
            temperature=0.4,
            max_tokens=2048,
        )

    @pytest.mark.asyncio
    async def test_returns_result(self, engine):
        result = await engine.deliberate(_context())
        assert isinstance(result, DeliberationResult)
        assert result.model == "glm-5.1"
        assert result.duration_ms > 0
        assert len(result.perspectives) == 3

    @pytest.mark.asyncio
    async def test_passes_model_and_temperature(self, engine):
        await engine.deliberate(_context())
        call = engine.provider.chat.call_args
        assert call.kwargs["model"] == "glm-5.1"
        assert call.kwargs["temperature"] == 0.4

    @pytest.mark.asyncio
    async def test_includes_failure_context_on_retry(self):
        provider = _mock_provider(_WELL_FORMED)
        engine = DeliberationEngine(provider=provider, model="glm-5.1")
        ctx = DeliberationContext(
            goal_summary="Fix bug",
            investigation_context="code context",
            previous_failure="Tests failed: AttributeError",
        )
        await engine.deliberate(ctx)
        call_args = provider.chat.call_args
        user_msg = call_args.kwargs["messages"][1]["content"]
        assert "INTENTO PREVIO FALLIDO" in user_msg
        assert "AttributeError" in user_msg

    @pytest.mark.asyncio
    async def test_truncates_long_context(self):
        provider = _mock_provider(_WELL_FORMED)
        engine = DeliberationEngine(provider=provider, model="glm-5.1")
        ctx = DeliberationContext(
            goal_summary="Fix",
            investigation_context="x" * 10000,
        )
        await engine.deliberate(ctx)
        user_msg = provider.chat.call_args.kwargs["messages"][1]["content"]
        assert len(user_msg) < 5000
