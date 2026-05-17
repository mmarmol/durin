"""Synthesis — packages all perspectives as enrichment for the main LLM.

V2: No winner selection. All 3 perspectives are presented to the main model
so it can synthesize with full context. The value is in diversity of
viewpoints, not in pre-selecting one.
"""

from __future__ import annotations

from durin.deliberation.types import (
    GeneratorRole,
    SynthesisResult,
    Verdict,
)


def synthesize(
    verdict: Verdict,
    posture_snapshot: dict[str, float] | None = None,
) -> SynthesisResult:
    """Build multi-perspective enrichment from all generated proposals."""
    pragmatico = ""
    explorador = ""
    critico = ""

    for sp in verdict.all_proposals:
        content = sp.proposal.content.strip()[:250]
        if sp.proposal.role == GeneratorRole.PRAGMATICO:
            pragmatico = content
        elif sp.proposal.role == GeneratorRole.EXPLORADOR:
            explorador = content
        elif sp.proposal.role == GeneratorRole.CRITICO:
            critico = content

    direction = pragmatico or (explorador or critico)
    reasoning = critico
    alternatives = explorador

    return SynthesisResult(
        direction=direction,
        reasoning=reasoning,
        alternatives_brief=alternatives,
        confidence="alta",
        under_doubt=verdict.under_doubt,
    )


def render_synthesis(result: SynthesisResult) -> str:
    """Render all perspectives as enrichment text for injection."""
    parts = []
    if result.direction:
        parts.append(f"Perspectiva directa: {result.direction}")
    if result.alternatives_brief:
        parts.append(f"Perspectiva alternativa: {result.alternatives_brief}")
    if result.reasoning:
        parts.append(f"Riesgos a considerar: {result.reasoning}")
    return "\n".join(parts)
