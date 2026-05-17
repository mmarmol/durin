"""Synthesis — renders deliberation result for context injection."""

from __future__ import annotations

from durin.deliberation.types import DeliberationResult


_DELIBERATION_TAG = "[Deliberación pre-análisis]"

_ROLE_LABELS = {
    "critico": "Riesgos identificados",
    "explorador": "Alternativa considerada",
    "pragmatico": "Enfoque directo",
}


def render_for_injection(result: DeliberationResult) -> str:
    """Format deliberation output for system message injection."""
    parts = [_DELIBERATION_TAG, ""]

    for p in result.perspectives:
        label = _ROLE_LABELS.get(p.role, p.role.capitalize())
        parts.append(f"{label}: {p.content}")

    if result.synthesis:
        parts.append(f"\nSíntesis: {result.synthesis}")

    return "\n".join(parts)
