"""Director — pure function that selects the winning proposal."""

from __future__ import annotations

from durin.deliberation.scoring import compute_final_score, compute_threshold
from durin.deliberation.types import (
    EvaluationScore,
    Proposal,
    ScoredProposal,
    Verdict,
)

_NEUTRAL_SCORE = 0.5


def decide(
    proposals: list[Proposal],
    evaluations: dict[str, list[EvaluationScore]],
    cautela: float,
    profundidad: float,
    round_number: int,
    max_rounds: int = 3,
) -> Verdict:
    threshold = compute_threshold(profundidad)
    scored: list[ScoredProposal] = []

    for proposal in proposals:
        key = f"{proposal.role}:{proposal.round_number}"
        scores = evaluations.get(key, [])
        avance = _find_score(scores, "avance")
        reversibilidad = _find_score(scores, "reversibilidad")
        final = compute_final_score(avance, reversibilidad, cautela)
        scored.append(ScoredProposal(
            proposal=proposal,
            scores=tuple(scores),
            final_score=final,
        ))

    scored.sort(key=lambda sp: sp.final_score, reverse=True)
    winner = scored[0]
    is_final_round = round_number >= max_rounds
    accepted = winner.final_score >= threshold or is_final_round

    return Verdict(
        winner=winner,
        accepted=accepted,
        threshold=threshold,
        all_proposals=tuple(scored),
        rounds_used=round_number,
        under_doubt=is_final_round and winner.final_score < threshold,
    )


def _find_score(scores: list[EvaluationScore], evaluator_name: str) -> float:
    for s in scores:
        if s.evaluator_name == evaluator_name:
            return s.score
    return _NEUTRAL_SCORE
