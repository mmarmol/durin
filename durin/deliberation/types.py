"""Deliberation system data types — all immutable."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GeneratorRole(StrEnum):
    PRAGMATICO = "pragmatico"
    EXPLORADOR = "explorador"
    CRITICO = "critico"
    HIBRIDO = "hibrido"


class ConvergenceReason(StrEnum):
    THRESHOLD = "threshold"
    PLATEAU = "plateau"
    MAX_ROUNDS = "max_rounds"


class TriggerReason(StrEnum):
    PLANNING_MOMENT = "planning_moment"
    DECISION_OUTSIDE_PLAN = "decision_outside_plan"
    CRITICAL_ACTION = "critical_action"


@dataclass(frozen=True, slots=True)
class Proposal:
    role: GeneratorRole
    content: str
    round_number: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvaluationScore:
    evaluator_name: str
    score: float
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class ScoredProposal:
    proposal: Proposal
    scores: tuple[EvaluationScore, ...]
    final_score: float


@dataclass(frozen=True, slots=True)
class Verdict:
    winner: ScoredProposal
    accepted: bool
    threshold: float
    all_proposals: tuple[ScoredProposal, ...]
    rounds_used: int
    under_doubt: bool = False
    convergence_reason: ConvergenceReason = ConvergenceReason.THRESHOLD


@dataclass(frozen=True, slots=True)
class RoundResult:
    proposals: tuple[ScoredProposal, ...]
    winner: ScoredProposal
    round_number: int


@dataclass(frozen=True, slots=True)
class DeliberationContext:
    trigger: TriggerReason
    goal_summary: str
    recent_context: str
    posture_snapshot: dict[str, float] = field(default_factory=dict)
    conversation_summary: str = ""
    active_objective: str = ""
    previous_verdict_brief: str = ""


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    direction: str
    reasoning: str
    alternatives_brief: str
    confidence: str
    under_doubt: bool


@dataclass(frozen=True, slots=True)
class VerdictEntry:
    timestamp: float
    trigger: TriggerReason
    winner_role: GeneratorRole
    winner_score: float
    threshold: float
    under_doubt: bool
    posture_snapshot: dict[str, float] = field(default_factory=dict)
    synthesis_brief: str = ""
