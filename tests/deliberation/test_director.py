"""Tests for director decision function."""

from __future__ import annotations

import pytest

from durin.deliberation.director import decide
from durin.deliberation.types import EvaluationScore, GeneratorRole, Proposal


def _proposal(role: GeneratorRole = GeneratorRole.PRAGMATICO, round_num: int = 1) -> Proposal:
    return Proposal(role=role, content=f"{role} proposal", round_number=round_num)


def _scores(avance: float, reversibilidad: float, key: str) -> dict[str, list[EvaluationScore]]:
    return {
        key: [
            EvaluationScore(evaluator_name="avance", score=avance),
            EvaluationScore(evaluator_name="reversibilidad", score=reversibilidad),
        ],
    }


class TestDirectorBasics:
    def test_high_scoring_accepted(self):
        p = _proposal()
        evals = _scores(0.9, 0.9, "pragmatico:1")
        verdict = decide([p], evals, cautela=0.5, profundidad=0.5, round_number=1)
        assert verdict.accepted is True
        assert verdict.winner.proposal == p
        assert verdict.under_doubt is False

    def test_low_scoring_not_accepted(self):
        p = _proposal()
        evals = _scores(0.2, 0.2, "pragmatico:1")
        verdict = decide([p], evals, cautela=0.5, profundidad=0.5, round_number=1)
        assert verdict.accepted is False
        assert verdict.under_doubt is False

    def test_final_round_always_accepts(self):
        p = _proposal()
        evals = _scores(0.2, 0.2, "pragmatico:1")
        verdict = decide([p], evals, cautela=0.5, profundidad=0.5, round_number=3, max_rounds=3)
        assert verdict.accepted is True
        assert verdict.under_doubt is True

    def test_rounds_used_tracked(self):
        p = _proposal()
        evals = _scores(0.9, 0.9, "pragmatico:1")
        verdict = decide([p], evals, cautela=0.5, profundidad=0.5, round_number=2)
        assert verdict.rounds_used == 2


class TestCautelaInfluence:
    def test_high_cautela_favors_safe_proposal(self):
        risky = _proposal(GeneratorRole.PRAGMATICO)
        safe = _proposal(GeneratorRole.CRITICO)
        evals = {
            "pragmatico:1": [
                EvaluationScore("avance", 0.9),
                EvaluationScore("reversibilidad", 0.2),
            ],
            "critico:1": [
                EvaluationScore("avance", 0.3),
                EvaluationScore("reversibilidad", 0.9),
            ],
        }
        verdict = decide([risky, safe], evals, cautela=0.9, profundidad=0.5, round_number=1)
        assert verdict.winner.proposal.role == GeneratorRole.CRITICO

    def test_low_cautela_favors_progress_proposal(self):
        risky = _proposal(GeneratorRole.PRAGMATICO)
        safe = _proposal(GeneratorRole.CRITICO)
        evals = {
            "pragmatico:1": [
                EvaluationScore("avance", 0.9),
                EvaluationScore("reversibilidad", 0.2),
            ],
            "critico:1": [
                EvaluationScore("avance", 0.3),
                EvaluationScore("reversibilidad", 0.9),
            ],
        }
        verdict = decide([risky, safe], evals, cautela=0.1, profundidad=0.5, round_number=1)
        assert verdict.winner.proposal.role == GeneratorRole.PRAGMATICO

    def test_neutral_cautela_balanced(self):
        p1 = _proposal(GeneratorRole.PRAGMATICO)
        p2 = _proposal(GeneratorRole.EXPLORADOR)
        evals = {
            "pragmatico:1": [
                EvaluationScore("avance", 0.7),
                EvaluationScore("reversibilidad", 0.5),
            ],
            "explorador:1": [
                EvaluationScore("avance", 0.5),
                EvaluationScore("reversibilidad", 0.7),
            ],
        }
        verdict = decide([p1, p2], evals, cautela=0.5, profundidad=0.5, round_number=1)
        assert verdict.winner.final_score == pytest.approx(0.6)


class TestProfundidadThreshold:
    def test_high_profundidad_harder_to_pass(self):
        p = _proposal()
        evals = _scores(0.6, 0.6, "pragmatico:1")
        verdict = decide([p], evals, cautela=0.5, profundidad=0.9, round_number=1)
        assert verdict.threshold == pytest.approx(0.67)
        assert verdict.accepted is False

    def test_low_profundidad_easier_to_pass(self):
        p = _proposal()
        evals = _scores(0.5, 0.5, "pragmatico:1")
        verdict = decide([p], evals, cautela=0.5, profundidad=0.1, round_number=1)
        assert verdict.threshold == pytest.approx(0.43)
        assert verdict.accepted is True


class TestEdgeCases:
    def test_empty_evaluations_uses_neutral(self):
        p = _proposal()
        verdict = decide([p], {}, cautela=0.5, profundidad=0.5, round_number=1)
        assert verdict.winner.final_score == pytest.approx(0.5)

    def test_multiple_proposals_sorted(self):
        proposals = [
            _proposal(GeneratorRole.PRAGMATICO),
            _proposal(GeneratorRole.EXPLORADOR),
            _proposal(GeneratorRole.CRITICO),
        ]
        evals = {
            "pragmatico:1": [EvaluationScore("avance", 0.5), EvaluationScore("reversibilidad", 0.5)],
            "explorador:1": [EvaluationScore("avance", 0.9), EvaluationScore("reversibilidad", 0.9)],
            "critico:1": [EvaluationScore("avance", 0.3), EvaluationScore("reversibilidad", 0.3)],
        }
        verdict = decide(proposals, evals, cautela=0.5, profundidad=0.5, round_number=1)
        assert verdict.winner.proposal.role == GeneratorRole.EXPLORADOR
        assert len(verdict.all_proposals) == 3
        scores = [sp.final_score for sp in verdict.all_proposals]
        assert scores == sorted(scores, reverse=True)

    def test_single_evaluator_missing(self):
        p = _proposal()
        evals = {"pragmatico:1": [EvaluationScore("avance", 0.8)]}
        verdict = decide([p], evals, cautela=0.5, profundidad=0.5, round_number=1)
        # Missing reversibilidad defaults to 0.5
        expected = 0.5 * 0.8 + 0.5 * 0.5
        assert verdict.winner.final_score == pytest.approx(expected)
