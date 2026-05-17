"""Tests for DeliberationService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.deliberation.engine import DeliberationEngine
from durin.deliberation.service import DeliberationService
from durin.deliberation.types import (
    DeliberationContext,
    DeliberationResult,
    Perspective,
)
from durin.providers.base import LLMResponse


_RESPONSE_TEXT = """\
[CRITICO]
Risk identified.

[EXPLORADOR]
Alternative approach.

[PRAGMATICO]
Direct path.

[SINTESIS]
Merged recommendation.
"""


@pytest.fixture
def service():
    provider = AsyncMock()
    provider.chat.return_value = LLMResponse(content=_RESPONSE_TEXT)
    engine = DeliberationEngine(provider=provider, model="glm-5.1")
    telemetry = MagicMock()
    return DeliberationService(engine=engine, telemetry=telemetry)


@pytest.fixture
def context():
    return DeliberationContext(
        goal_summary="Fix the bug",
        investigation_context="Found the issue in module X",
        posture_snapshot={"cautela": 0.6},
    )


class TestDeliberate:
    @pytest.mark.asyncio
    async def test_returns_result(self, service, context):
        result = await service.deliberate(context)
        assert isinstance(result, DeliberationResult)
        assert len(result.perspectives) == 3
        assert "Merged" in result.synthesis

    @pytest.mark.asyncio
    async def test_logs_to_telemetry(self, service, context):
        await service.deliberate(context)
        service._telemetry.log_deliberation_v3.assert_called_once()
        call_kwargs = service._telemetry.log_deliberation_v3.call_args.kwargs
        assert call_kwargs["trigger"] == "investigate_to_plan"
        assert call_kwargs["cycle"] == 1
        assert "critico" in call_kwargs["perspectives"]

    @pytest.mark.asyncio
    async def test_tracks_history(self, service, context):
        await service.deliberate(context)
        assert len(service.history) == 1
        assert service.history[0].trigger == "investigate_to_plan"

    @pytest.mark.asyncio
    async def test_render(self, service, context):
        result = await service.deliberate(context)
        rendered = service.render(result)
        assert "[Deliberación pre-análisis]" in rendered
        assert "Riesgos identificados" in rendered
