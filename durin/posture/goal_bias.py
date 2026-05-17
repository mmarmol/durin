"""Goal-sensitive initialization — keyword heuristics to pre-bias posture at cold start.

When a goal arrives, inspect it with simple rules to nudge the vector
before the first step runs.
"""

from __future__ import annotations

from durin.posture.vector import AxisName

_CAUTION_UP: frozenset[str] = frozenset({
    "production", "deploy", "irreversible", "critical",
    "delete", "drop", "rm -rf", "force push", "destructive",
    "migration",
})

_EXPLORATION_UP: frozenset[str] = frozenset({
    "research", "explore", "brainstorm", "alternatives",
    "what options", "possibilities", "options",
})

_DISCIPLINE_UP: frozenset[str] = frozenset({
    "protocol", "procedure", "step by step", "checklist",
    "standard", "compliance", "regulation",
})

_GOAL_BIAS_DELTA = 0.10


def compute_goal_bias(goal_text: str) -> dict[AxisName, float]:
    """Deterministic keyword scan of the goal text to produce cold-start deltas.

    Returns a dict of axis -> delta. Only axes triggered by keywords appear.
    No LLM call — pure string matching.
    """
    text = goal_text.lower()
    deltas: dict[AxisName, float] = {}

    if any(kw in text for kw in _CAUTION_UP):
        deltas[AxisName.CAUTION] = _GOAL_BIAS_DELTA

    if any(kw in text for kw in _EXPLORATION_UP):
        deltas[AxisName.EXPLORATION] = _GOAL_BIAS_DELTA

    if any(kw in text for kw in _DISCIPLINE_UP):
        deltas[AxisName.DISCIPLINE] = _GOAL_BIAS_DELTA

    return deltas
