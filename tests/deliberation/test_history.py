"""Tests for VerdictHistory — ring buffer, serialization, pattern detection."""

from __future__ import annotations

import time

import pytest

from durin.deliberation.history import VerdictHistory, _MAX_ENTRIES
from durin.deliberation.types import GeneratorRole, TriggerReason, VerdictEntry


def _entry(
    role: GeneratorRole = GeneratorRole.PRAGMATICO,
    score: float = 0.7,
    threshold: float = 0.55,
    under_doubt: bool = False,
    trigger: TriggerReason = TriggerReason.PLANNING_MOMENT,
) -> VerdictEntry:
    return VerdictEntry(
        timestamp=time.time(),
        trigger=trigger,
        winner_role=role,
        winner_score=score,
        threshold=threshold,
        under_doubt=under_doubt,
        posture_snapshot={"cautela": 0.5},
        synthesis_brief="some direction",
    )


class TestAppendAndRetrieve:
    def test_append_and_entries(self):
        h = VerdictHistory()
        e = _entry()
        h.append(e)
        assert h.entries == [e]
        assert len(h) == 1

    def test_last_returns_most_recent(self):
        h = VerdictHistory()
        e1 = _entry(role=GeneratorRole.PRAGMATICO)
        e2 = _entry(role=GeneratorRole.EXPLORADOR)
        h.append(e1)
        h.append(e2)
        assert h.last == e2

    def test_last_none_when_empty(self):
        h = VerdictHistory()
        assert h.last is None

    def test_entries_returns_copy(self):
        h = VerdictHistory()
        h.append(_entry())
        entries = h.entries
        entries.clear()
        assert len(h) == 1


class TestRingBuffer:
    def test_drops_oldest_at_max(self):
        entries = [_entry(score=float(i)) for i in range(_MAX_ENTRIES + 5)]
        h = VerdictHistory(entries)
        assert len(h) == _MAX_ENTRIES
        assert h.entries[0].winner_score == 5.0

    def test_append_beyond_max(self):
        h = VerdictHistory([_entry() for _ in range(_MAX_ENTRIES)])
        new = _entry(role=GeneratorRole.CRITICO)
        h.append(new)
        assert len(h) == _MAX_ENTRIES
        assert h.last == new


class TestDominantRole:
    def test_majority_detected(self):
        h = VerdictHistory()
        for _ in range(4):
            h.append(_entry(role=GeneratorRole.PRAGMATICO))
        h.append(_entry(role=GeneratorRole.EXPLORADOR))
        assert h.dominant_role() == GeneratorRole.PRAGMATICO

    def test_none_under_3_entries(self):
        h = VerdictHistory()
        h.append(_entry(role=GeneratorRole.PRAGMATICO))
        h.append(_entry(role=GeneratorRole.PRAGMATICO))
        assert h.dominant_role() is None

    def test_window_limits_scope(self):
        h = VerdictHistory()
        for _ in range(10):
            h.append(_entry(role=GeneratorRole.PRAGMATICO))
        for _ in range(4):
            h.append(_entry(role=GeneratorRole.EXPLORADOR))
        h.append(_entry(role=GeneratorRole.CRITICO))
        assert h.dominant_role(window=5) == GeneratorRole.EXPLORADOR

    def test_role_distribution(self):
        h = VerdictHistory()
        h.append(_entry(role=GeneratorRole.PRAGMATICO))
        h.append(_entry(role=GeneratorRole.PRAGMATICO))
        h.append(_entry(role=GeneratorRole.EXPLORADOR))
        dist = h.role_distribution()
        assert dist[GeneratorRole.PRAGMATICO] == 2
        assert dist[GeneratorRole.EXPLORADOR] == 1


class TestSerializeDeserialize:
    def test_roundtrip(self):
        h = VerdictHistory()
        h.append(_entry(role=GeneratorRole.PRAGMATICO, score=0.72))
        h.append(_entry(role=GeneratorRole.EXPLORADOR, score=0.61, under_doubt=True))

        data = h.serialize()
        h2 = VerdictHistory.deserialize(data)

        assert len(h2) == 2
        assert h2.entries[0].winner_role == GeneratorRole.PRAGMATICO
        assert h2.entries[0].winner_score == 0.72
        assert h2.entries[1].under_doubt is True

    def test_deserialize_skips_invalid(self):
        data = [
            {"timestamp": 1.0, "trigger": "planning_moment", "winner_role": "pragmatico",
             "winner_score": 0.7, "threshold": 0.55, "under_doubt": False},
            {"invalid": True},
            {"timestamp": 2.0, "trigger": "bad_trigger", "winner_role": "pragmatico",
             "winner_score": 0.5, "threshold": 0.4, "under_doubt": False},
        ]
        h = VerdictHistory.deserialize(data)
        assert len(h) == 1

    def test_serialize_empty(self):
        h = VerdictHistory()
        assert h.serialize() == []


class TestHookIntegration:
    @pytest.mark.asyncio
    async def test_hook_accumulates_history(self):
        from unittest.mock import AsyncMock
        from durin.agent.hook import AgentHookContext
        from durin.deliberation.engine import DeliberationEngine
        from durin.deliberation.evaluator import LLMEvaluator
        from durin.deliberation.generator import GeneratorConfig
        from durin.deliberation.hook import DeliberationHook
        from durin.providers.base import LLMResponse

        provider = AsyncMock()
        responses = ["usar JWT", "explorar passkeys", "OAuth2", "7", "8", "6", "5", "7", "9"]
        call_count = [0]

        async def _chat(**kwargs):
            idx = call_count[0] % len(responses)
            call_count[0] += 1
            return LLMResponse(content=responses[idx], tool_calls=[], finish_reason="stop", usage={})

        provider.chat = _chat

        generators = [
            GeneratorConfig(role=GeneratorRole.PRAGMATICO, model="m", temperature=0.3, prompt_template="t"),
            GeneratorConfig(role=GeneratorRole.EXPLORADOR, model="m", temperature=0.8, prompt_template="t"),
            GeneratorConfig(role=GeneratorRole.CRITICO, model="m", temperature=0.5, prompt_template="t"),
        ]
        evaluators = [
            LLMEvaluator("avance", provider, "m", "score"),
            LLMEvaluator("reversibilidad", provider, "m", "score"),
        ]
        engine = DeliberationEngine(
            provider=provider, generators=generators,
            evaluators=evaluators, max_rounds=1,
        )
        hook = DeliberationHook(engine=engine)

        ctx = AgentHookContext(
            iteration=0,
            messages=[
                {"role": "system", "content": "test"},
                {"role": "user", "content": "implementar auth"},
            ],
        )
        await hook.before_iteration(ctx)

        assert len(hook.history) == 1
        assert hook.history.last is not None
        assert hook.history.last.trigger == TriggerReason.PLANNING_MOMENT
