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
[CRITIC]
output_field = x rebinds the local variable, doesn't write to the array.

[EXPLORER]
Use np.copyto() for explicit memory writes without rebinding.

[PRAGMATIC]
Use output_field[...] = x to write through the view.

[SYNTHESIS]
The fix must use in-place assignment via [...] or copyto.
"""


class TestParseResponse:
    def test_parses_all_sections(self):
        result = DeliberationEngine._parse_response(_WELL_FORMED)
        assert len(result.perspectives) == 3
        assert result.perspectives[0].role == "critic"
        assert result.perspectives[1].role == "explorer"
        assert result.perspectives[2].role == "pragmatic"
        assert "in-place" in result.synthesis

    def test_fallback_on_unstructured(self):
        result = DeliberationEngine._parse_response("Just do the obvious thing.")
        assert len(result.perspectives) == 1
        assert result.perspectives[0].role == "pragmatic"
        assert result.synthesis == "Just do the obvious thing."

    def test_missing_synthesis_uses_last_perspective(self):
        text = "[CRITIC]\nRisk A.\n\n[EXPLORER]\nAlt B.\n\n[PRAGMATIC]\nDo C."
        result = DeliberationEngine._parse_response(text)
        assert len(result.perspectives) == 3
        assert result.synthesis == "Do C."

    def test_partial_sections(self):
        text = "[CRITIC]\nOnly risk here.\n\n[SYNTHESIS]\nJust this."
        result = DeliberationEngine._parse_response(text)
        assert len(result.perspectives) == 1
        assert result.perspectives[0].role == "critic"
        assert result.synthesis == "Just this."


class TestExtractSections:
    def test_basic(self):
        sections = _extract_sections("[CRITIC]\nA\n[EXPLORER]\nB")
        assert sections["critic"] == "A"
        assert sections["explorer"] == "B"

    def test_case_insensitive(self):
        sections = _extract_sections("[critic]\ntest")
        assert "critic" in sections

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
        assert "PREVIOUS FAILED ATTEMPT" in user_msg
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
