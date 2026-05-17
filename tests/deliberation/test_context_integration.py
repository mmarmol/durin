"""Tests for deliberation synthesis injection into the system prompt via hook."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from durin.agent.context import ContextBuilder
from durin.agent.hook import AgentHookContext
from durin.deliberation.engine import DeliberationEngine
from durin.deliberation.hook import DeliberationHook
from durin.deliberation.types import (
    GeneratorRole,
    Proposal,
    ScoredProposal,
    Verdict,
)


def _mock_engine(content: str = "usar OAuth2 existente") -> AsyncMock:
    engine = AsyncMock(spec=DeliberationEngine)
    sp = ScoredProposal(
        proposal=Proposal(role=GeneratorRole.PRAGMATICO, content=content, round_number=1),
        scores=(),
        final_score=0.7,
    )
    engine.deliberate.return_value = Verdict(
        winner=sp,
        accepted=True,
        threshold=0.55,
        all_proposals=(sp,),
        rounds_used=1,
        under_doubt=False,
    )
    return engine


class TestHookInjectsIntoContextBuilderOutput:
    """The hook patches the system prompt that ContextBuilder produced."""

    @pytest.mark.asyncio
    async def test_deliberation_injected_as_pre_message(self, tmp_path: Path):
        ctx_builder = ContextBuilder(tmp_path)
        messages = ctx_builder.build_messages(
            history=[],
            current_message="implement auth",
            posture_phrase="Priorizá reversibilidad.",
        )

        engine = _mock_engine("feature flag primero, rollback inmediato")
        hook = DeliberationHook(engine)
        hook_ctx = AgentHookContext(iteration=0, messages=messages)
        await hook.before_iteration(hook_ctx)

        # Posture stays in system prompt
        system_content = hook_ctx.messages[0]["content"]
        assert "# Postura" in system_content
        # Deliberation is a separate message
        delib_msgs = [m for m in hook_ctx.messages if "Deliberación pre-análisis" in m.get("content", "")]
        assert len(delib_msgs) == 1
        assert "feature flag primero" in delib_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_deliberation_as_separate_message(self, tmp_path: Path):
        ctx_builder = ContextBuilder(tmp_path)
        messages = ctx_builder.build_messages(
            history=[],
            current_message="deploy",
            posture_phrase="Sé audaz.",
        )

        engine = _mock_engine("deploy directo a prod")
        hook = DeliberationHook(engine)
        hook_ctx = AgentHookContext(iteration=0, messages=messages)
        await hook.before_iteration(hook_ctx)

        delib_msgs = [m for m in hook_ctx.messages if "Deliberación pre-análisis" in m.get("content", "")]
        assert len(delib_msgs) == 1
        assert "deploy directo a prod" in delib_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_no_deliberacion_when_engine_fails(self, tmp_path: Path):
        ctx_builder = ContextBuilder(tmp_path)
        messages = ctx_builder.build_messages(
            history=[],
            current_message="something",
        )

        engine = AsyncMock(spec=DeliberationEngine)
        engine.deliberate.side_effect = RuntimeError("ollama down")
        hook = DeliberationHook(engine)
        hook_ctx = AgentHookContext(iteration=0, messages=messages)
        await hook.before_iteration(hook_ctx)

        system_content = hook_ctx.messages[0]["content"]
        assert "# Deliberación" not in system_content

    @pytest.mark.asyncio
    async def test_no_postura_still_gets_deliberacion(self, tmp_path: Path):
        ctx_builder = ContextBuilder(tmp_path)
        messages = ctx_builder.build_messages(
            history=[],
            current_message="do task",
        )

        engine = _mock_engine("hacer lo simple")
        hook = DeliberationHook(engine)
        hook_ctx = AgentHookContext(iteration=0, messages=messages)
        await hook.before_iteration(hook_ctx)

        delib_msgs = [m for m in hook_ctx.messages if "Deliberación pre-análisis" in m.get("content", "")]
        assert len(delib_msgs) == 1
        assert "hacer lo simple" in delib_msgs[0]["content"]
