"""Tests for deliberation data types."""

from __future__ import annotations

import pytest

from durin.deliberation.types import (
    DeliberationContext,
    EvaluationScore,
    GeneratorRole,
    Proposal,
    ScoredProposal,
    TriggerReason,
    Verdict,
)


class TestGeneratorRole:
    def test_all_roles_exist(self):
        assert GeneratorRole.PRAGMATICO == "pragmatico"
        assert GeneratorRole.EXPLORADOR == "explorador"
        assert GeneratorRole.CRITICO == "critico"

    def test_has_four_roles(self):
        assert len(GeneratorRole) == 4
        assert GeneratorRole.HIBRIDO == "hibrido"


class TestTriggerReason:
    def test_all_triggers_exist(self):
        assert TriggerReason.PLANNING_MOMENT == "planning_moment"
        assert TriggerReason.DECISION_OUTSIDE_PLAN == "decision_outside_plan"
        assert TriggerReason.CRITICAL_ACTION == "critical_action"


class TestProposal:
    def test_construction(self):
        p = Proposal(
            role=GeneratorRole.PRAGMATICO,
            content="do the thing",
            round_number=1,
        )
        assert p.role == GeneratorRole.PRAGMATICO
        assert p.content == "do the thing"
        assert p.round_number == 1
        assert p.metadata == {}

    def test_immutable(self):
        p = Proposal(role=GeneratorRole.EXPLORADOR, content="x", round_number=1)
        with pytest.raises(Exception):
            p.content = "y"  # type: ignore[misc]

    def test_with_metadata(self):
        p = Proposal(
            role=GeneratorRole.CRITICO,
            content="careful",
            round_number=2,
            metadata={"usage": {"tokens": 50}},
        )
        assert p.metadata["usage"]["tokens"] == 50


class TestEvaluationScore:
    def test_construction(self):
        s = EvaluationScore(evaluator_name="avance", score=0.75, rationale="good")
        assert s.evaluator_name == "avance"
        assert s.score == 0.75
        assert s.rationale == "good"

    def test_default_rationale(self):
        s = EvaluationScore(evaluator_name="reversibilidad", score=0.5)
        assert s.rationale == ""

    def test_immutable(self):
        s = EvaluationScore(evaluator_name="avance", score=0.5)
        with pytest.raises(Exception):
            s.score = 0.9  # type: ignore[misc]


class TestScoredProposal:
    def test_construction(self):
        p = Proposal(role=GeneratorRole.PRAGMATICO, content="x", round_number=1)
        scores = (
            EvaluationScore(evaluator_name="avance", score=0.8),
            EvaluationScore(evaluator_name="reversibilidad", score=0.6),
        )
        sp = ScoredProposal(proposal=p, scores=scores, final_score=0.7)
        assert sp.final_score == 0.7
        assert len(sp.scores) == 2


class TestVerdict:
    def test_construction(self):
        p = Proposal(role=GeneratorRole.PRAGMATICO, content="x", round_number=1)
        sp = ScoredProposal(
            proposal=p,
            scores=(EvaluationScore("avance", 0.8),),
            final_score=0.8,
        )
        v = Verdict(
            winner=sp,
            accepted=True,
            threshold=0.55,
            all_proposals=(sp,),
            rounds_used=1,
        )
        assert v.accepted is True
        assert v.under_doubt is False
        assert v.rounds_used == 1

    def test_under_doubt(self):
        p = Proposal(role=GeneratorRole.CRITICO, content="y", round_number=3)
        sp = ScoredProposal(proposal=p, scores=(), final_score=0.4)
        v = Verdict(
            winner=sp,
            accepted=True,
            threshold=0.55,
            all_proposals=(sp,),
            rounds_used=3,
            under_doubt=True,
        )
        assert v.under_doubt is True


class TestDeliberationContext:
    def test_construction(self):
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="implement auth",
            recent_context="user asked for login page",
            posture_snapshot={"cautela": 0.6, "exploracion": 0.4},
        )
        assert ctx.trigger == TriggerReason.PLANNING_MOMENT
        assert ctx.posture_snapshot["cautela"] == 0.6

    def test_default_snapshot(self):
        ctx = DeliberationContext(
            trigger=TriggerReason.CRITICAL_ACTION,
            goal_summary="deploy",
            recent_context="",
        )
        assert ctx.posture_snapshot == {}

    def test_immutable(self):
        ctx = DeliberationContext(
            trigger=TriggerReason.PLANNING_MOMENT,
            goal_summary="x",
            recent_context="y",
        )
        with pytest.raises(Exception):
            ctx.goal_summary = "z"  # type: ignore[misc]
