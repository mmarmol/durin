"""Synthesis — packages deliberation results into enrichment for the main LLM."""

from __future__ import annotations

from durin.deliberation.types import (
    ConvergenceReason,
    GeneratorRole,
    SynthesisResult,
    Verdict,
)


_DOUBT_NOTE = "⚠ Esta recomendación no alcanzó plena confianza."


def synthesize(
    verdict: Verdict,
    posture_snapshot: dict[str, float] | None = None,
) -> SynthesisResult:
    """Build a structured synthesis from the verdict and posture state.

    Pure function — no I/O.  Produces multi-perspective enrichment:
    approach (evolved winner), risks (from critic), alternative (runner-up).
    """
    approach = verdict.winner.proposal.content.strip()
    risks = _extract_risks(verdict)
    alternative = _extract_alternative(verdict)
    confidence = _compute_confidence(verdict)

    return SynthesisResult(
        direction=approach,
        reasoning=risks or _build_reasoning(verdict, posture_snapshot),
        alternatives_brief=alternative,
        confidence=confidence,
        under_doubt=verdict.under_doubt,
    )


def render_synthesis(result: SynthesisResult) -> str:
    """Render SynthesisResult as enrichment text for injection."""
    parts = [f"Enfoque recomendado: {result.direction}"]
    if result.reasoning:
        parts.append(f"Riesgos identificados: {result.reasoning}")
    if result.alternatives_brief:
        parts.append(f"Alternativa considerada: {result.alternatives_brief}")
    parts.append(f"Confianza: {result.confidence}")
    if result.under_doubt:
        parts.append(_DOUBT_NOTE)
    return "\n".join(parts)


def _extract_risks(verdict: Verdict) -> str:
    """Extract risk insights from the critic's proposal if present."""
    for sp in verdict.all_proposals:
        if sp.proposal.role == GeneratorRole.CRITICO and sp.proposal.content.strip():
            return sp.proposal.content.strip()[:200]
    return ""


def _extract_alternative(verdict: Verdict) -> str:
    """Get the best runner-up's content as alternative perspective."""
    others = sorted(
        [sp for sp in verdict.all_proposals if sp is not verdict.winner],
        key=lambda sp: sp.final_score,
        reverse=True,
    )
    if others and others[0].proposal.content.strip():
        role = others[0].proposal.role
        return f"{role}: {others[0].proposal.content.strip()[:150]}"
    return ""


def _build_reasoning(verdict: Verdict, posture: dict[str, float] | None) -> str:
    parts = []
    if posture:
        cautela = posture.get("cautela", 0.5)
        if cautela >= 0.65:
            parts.append(f"cautela alta ({cautela:.2f}) priorizó reversibilidad")
        elif cautela <= 0.35:
            parts.append(f"cautela baja ({cautela:.2f}) priorizó avance")

    parts.append(f"score {verdict.winner.final_score:.2f} vs umbral {verdict.threshold:.2f}")
    return "; ".join(parts)


def _compute_confidence(verdict: Verdict) -> str:
    if verdict.under_doubt:
        return "baja"
    ratio = verdict.winner.final_score / verdict.threshold if verdict.threshold > 0 else 1.0
    if ratio >= 1.3:
        return "alta"
    return "media"
