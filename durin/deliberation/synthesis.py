"""Synthesis — renders deliberation result for context injection."""

from __future__ import annotations

from durin.deliberation.types import DeliberationResult


_DELIBERATION_TAG = "[Pre-analysis deliberation]"

_ROLE_LABELS = {
    "critic": "Risks identified",
    "explorer": "Alternative considered",
    "pragmatic": "Direct approach",
}


def render_for_injection(result: DeliberationResult) -> str:
    """Format deliberation output for system message injection."""
    parts = [_DELIBERATION_TAG, ""]

    for p in result.perspectives:
        label = _ROLE_LABELS.get(p.role, p.role.capitalize())
        parts.append(f"{label}: {p.content}")

    if result.synthesis:
        parts.append(f"\nSynthesis: {result.synthesis}")

    return "\n".join(parts)
