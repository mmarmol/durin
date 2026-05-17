"""Durin deliberation system — multi-generator proposal pipeline."""

from durin.deliberation.config import (
    DeliberationConfig,
    EvaluatorConfig,
    GeneratorRoleConfig,
)
from durin.deliberation.director import decide
from durin.deliberation.engine import DeliberationEngine
from durin.deliberation.evaluator import Evaluator, LLMEvaluator
from durin.deliberation.generator import GeneratorConfig, generate_proposal
from durin.deliberation.hook import DeliberationHook
from durin.deliberation.scoring import (
    compute_final_score,
    compute_threshold,
    compute_weight_avance,
    compute_weight_reversibilidad,
)
from durin.deliberation.history import VerdictHistory
from durin.deliberation.synthesis import render_synthesis, synthesize
from durin.deliberation.types import (
    DeliberationContext,
    EvaluationScore,
    GeneratorRole,
    Proposal,
    ScoredProposal,
    SynthesisResult,
    TriggerReason,
    Verdict,
    VerdictEntry,
)

__all__ = [
    "DeliberationConfig",
    "DeliberationContext",
    "DeliberationEngine",
    "DeliberationHook",
    "EvaluationScore",
    "Evaluator",
    "EvaluatorConfig",
    "GeneratorConfig",
    "GeneratorRole",
    "GeneratorRoleConfig",
    "LLMEvaluator",
    "Proposal",
    "ScoredProposal",
    "SynthesisResult",
    "TriggerReason",
    "Verdict",
    "VerdictEntry",
    "VerdictHistory",
    "compute_final_score",
    "compute_threshold",
    "compute_weight_avance",
    "compute_weight_reversibilidad",
    "decide",
    "generate_proposal",
    "render_synthesis",
    "synthesize",
]
