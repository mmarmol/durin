"""Goal-sensitive initialization — keyword heuristics to pre-bias posture at cold start.

Implements doc §3.4: when a goal arrives, inspect it with simple rules
to nudge the vector before the first step runs.
"""

from __future__ import annotations

from durin.posture.vector import AxisName

_CAUTELA_UP: frozenset[str] = frozenset({
    "producción", "production", "deploy", "irreversible", "crítico",
    "critical", "delete", "drop", "rm -rf", "force push", "destructive",
    "migration", "migración",
})

_EXPLORACION_UP: frozenset[str] = frozenset({
    "investigá", "explorá", "buscá opciones", "alternativas",
    "qué opciones", "research", "explore", "brainstorm",
    "posibilidades", "opciones",
})

_DISCIPLINA_UP: frozenset[str] = frozenset({
    "protocolo", "procedimiento", "paso a paso", "checklist",
    "standard", "compliance", "normativa", "regulación",
})

_GOAL_BIAS_DELTA = 0.10


def compute_goal_bias(goal_text: str) -> dict[AxisName, float]:
    """Deterministic keyword scan of the goal text to produce cold-start deltas.

    Returns a dict of axis → delta. Only axes triggered by keywords appear.
    No LLM call — pure string matching.
    """
    text = goal_text.lower()
    deltas: dict[AxisName, float] = {}

    if any(kw in text for kw in _CAUTELA_UP):
        deltas[AxisName.CAUTELA] = _GOAL_BIAS_DELTA

    if any(kw in text for kw in _EXPLORACION_UP):
        deltas[AxisName.EXPLORACION] = _GOAL_BIAS_DELTA

    if any(kw in text for kw in _DISCIPLINA_UP):
        deltas[AxisName.DISCIPLINA] = _GOAL_BIAS_DELTA

    return deltas
