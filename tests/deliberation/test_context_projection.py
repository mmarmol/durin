"""Tests for context projection — enriched generator input."""

from __future__ import annotations

import time

import pytest

from durin.agent.hook import AgentHookContext
from durin.deliberation.generator import _build_user_prompt
from durin.deliberation.history import VerdictHistory
from durin.deliberation.hook import DeliberationHook
from durin.deliberation.types import (
    DeliberationContext,
    GeneratorRole,
    TriggerReason,
    VerdictEntry,
)


class TestBuildUserPrompt:
    def test_includes_goal_summary(self):
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="implementar auth",
            recent_context="",
        )
        prompt = _build_user_prompt(ctx)
        assert "Objetivo: implementar auth" in prompt

    def test_includes_active_objective(self):
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="next step",
            recent_context="",
            active_objective="Build complete login system",
        )
        prompt = _build_user_prompt(ctx)
        assert "Objetivo sostenido: Build complete login system" in prompt

    def test_includes_conversation_summary(self):
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="next step",
            recent_context="",
            conversation_summary="created user model | added migration | wrote tests",
        )
        prompt = _build_user_prompt(ctx)
        assert "Resumen reciente: created user model | added migration | wrote tests" in prompt

    def test_includes_previous_verdict(self):
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="next step",
            recent_context="",
            previous_verdict_brief="pragmatico (0.72): usar JWT con refresh tokens",
        )
        prompt = _build_user_prompt(ctx)
        assert "Decisión anterior: pragmatico (0.72): usar JWT con refresh tokens" in prompt

    def test_includes_recent_context(self):
        ctx = DeliberationContext(
            trigger=TriggerReason.CRITICAL_ACTION,
            goal_summary="deploy",
            recent_context="Tools a ejecutar: git_push, deploy",
        )
        prompt = _build_user_prompt(ctx)
        assert "Contexto: Tools a ejecutar: git_push, deploy" in prompt

    def test_graceful_with_empty_fields(self):
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="do something",
            recent_context="",
        )
        prompt = _build_user_prompt(ctx)
        assert "Objetivo: do something" in prompt
        assert "Objetivo sostenido" not in prompt
        assert "Resumen reciente" not in prompt
        assert "Decisión anterior" not in prompt
        assert "Contexto" not in prompt


class TestHookContextExtraction:
    def test_conversation_summary_from_messages(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hola"},
            {"role": "assistant", "content": "Hello! I created the user model."},
            {"role": "user", "content": "ahora la migration"},
            {"role": "assistant", "content": "Done! Migration added for users table."},
            {"role": "user", "content": "tests?"},
            {"role": "assistant", "content": "Tests written and passing."},
        ]
        summary = DeliberationHook._extract_conversation_summary(
            AgentHookContext(iteration=0, messages=messages)
        )
        assert "Hello! I created the user model." in summary
        assert "Done! Migration added" in summary
        assert "Tests written" in summary
        assert " | " in summary

    def test_conversation_summary_truncates_long_messages(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "x" * 200},
        ]
        summary = DeliberationHook._extract_conversation_summary(
            AgentHookContext(iteration=0, messages=messages)
        )
        assert len(summary) <= 100

    def test_conversation_summary_empty_without_assistant(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ]
        summary = DeliberationHook._extract_conversation_summary(
            AgentHookContext(iteration=0, messages=messages)
        )
        assert summary == ""

    def test_active_objective_from_system_prompt(self):
        messages = [
            {"role": "system", "content": "You are helpful.\n\nGoal (active):\nImplement login system"},
        ]
        objective = DeliberationHook._extract_active_objective(
            AgentHookContext(iteration=0, messages=messages)
        )
        assert objective == "Implement login system"

    def test_active_objective_empty_without_marker(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
        ]
        objective = DeliberationHook._extract_active_objective(
            AgentHookContext(iteration=0, messages=messages)
        )
        assert objective == ""

    def test_format_previous_verdict_from_history(self):
        from unittest.mock import AsyncMock
        from durin.deliberation.engine import DeliberationEngine
        from durin.deliberation.evaluator import LLMEvaluator
        from durin.deliberation.generator import GeneratorConfig

        provider = AsyncMock()
        engine = DeliberationEngine(
            provider=provider,
            generators=[GeneratorConfig(role=GeneratorRole.PRAGMATICO, model="m", temperature=0.3, prompt_template="t")],
            evaluators=[LLMEvaluator("avance", provider, "m", "score")],
            max_rounds=1,
        )

        history = VerdictHistory()
        history.append(VerdictEntry(
            timestamp=time.time(),
            trigger=TriggerReason.PLANNING_MOMENT,
            winner_role=GeneratorRole.PRAGMATICO,
            winner_score=0.72,
            threshold=0.55,
            under_doubt=False,
            posture_snapshot={"cautela": 0.5},
            synthesis_brief="usar JWT con refresh tokens",
        ))

        hook = DeliberationHook(engine=engine, history=history)
        brief = hook._format_previous_verdict()
        assert "pragmatico (0.72)" in brief
        assert "usar JWT con refresh tokens" in brief

    def test_format_previous_verdict_empty_without_history(self):
        from unittest.mock import AsyncMock
        from durin.deliberation.engine import DeliberationEngine
        from durin.deliberation.evaluator import LLMEvaluator
        from durin.deliberation.generator import GeneratorConfig

        provider = AsyncMock()
        engine = DeliberationEngine(
            provider=provider,
            generators=[GeneratorConfig(role=GeneratorRole.PRAGMATICO, model="m", temperature=0.3, prompt_template="t")],
            evaluators=[LLMEvaluator("avance", provider, "m", "score")],
            max_rounds=1,
        )

        hook = DeliberationHook(engine=engine)
        assert hook._format_previous_verdict() == ""
