"""Tests for deliberation synthesis — structured reasoning + rendering."""

from __future__ import annotations

from durin.deliberation.synthesis import render_synthesis, synthesize
from durin.deliberation.types import (
    GeneratorRole,
    Proposal,
    ScoredProposal,
    SynthesisResult,
    Verdict,
)


def _verdict(
    role: GeneratorRole = GeneratorRole.PRAGMATICO,
    content: str = "hacer X",
    score: float = 0.7,
    threshold: float = 0.55,
    under_doubt: bool = False,
    others: list[tuple[GeneratorRole, float, str]] | None = None,
) -> Verdict:
    winner = ScoredProposal(
        proposal=Proposal(role=role, content=content, round_number=1),
        scores=(),
        final_score=score,
    )
    all_proposals = [winner]
    if others:
        for other_role, other_score, other_content in others:
            all_proposals.append(ScoredProposal(
                proposal=Proposal(role=other_role, content=other_content, round_number=1),
                scores=(),
                final_score=other_score,
            ))
    return Verdict(
        winner=winner,
        accepted=True,
        threshold=threshold,
        all_proposals=tuple(all_proposals),
        rounds_used=1,
        under_doubt=under_doubt,
    )


class TestSynthesizeResult:
    def test_returns_synthesis_result(self):
        result = synthesize(_verdict())
        assert isinstance(result, SynthesisResult)

    def test_direction_is_proposal_content_stripped(self):
        result = synthesize(_verdict(content="  usar JWT  "))
        assert result.direction == "usar JWT"

    def test_confidence_alta_when_score_exceeds_threshold_by_30pct(self):
        result = synthesize(_verdict(score=0.80, threshold=0.55))
        assert result.confidence == "alta"

    def test_confidence_media_when_score_exceeds_threshold_under_30pct(self):
        result = synthesize(_verdict(score=0.60, threshold=0.55))
        assert result.confidence == "media"

    def test_confidence_baja_when_under_doubt(self):
        result = synthesize(_verdict(under_doubt=True))
        assert result.confidence == "baja"

    def test_under_doubt_flag_propagates(self):
        assert synthesize(_verdict(under_doubt=False)).under_doubt is False
        assert synthesize(_verdict(under_doubt=True)).under_doubt is True


class TestSynthesizeReasoning:
    def test_reasoning_uses_critic_content_when_available(self):
        result = synthesize(_verdict(
            others=[(GeneratorRole.CRITICO, 0.5, "riesgo de timeout en la DB")],
        ))
        assert "riesgo de timeout" in result.reasoning

    def test_reasoning_falls_back_to_score_when_no_critic(self):
        result = synthesize(_verdict(score=0.72, threshold=0.55))
        assert "0.72" in result.reasoning

    def test_reasoning_mentions_cautela_when_high_and_no_critic(self):
        result = synthesize(_verdict(), posture_snapshot={"cautela": 0.80})
        assert "cautela alta" in result.reasoning
        assert "reversibilidad" in result.reasoning

    def test_reasoning_mentions_cautela_when_low_and_no_critic(self):
        result = synthesize(_verdict(), posture_snapshot={"cautela": 0.20})
        assert "cautela baja" in result.reasoning
        assert "avance" in result.reasoning


class TestSynthesizeAlternatives:
    def test_alternatives_empty_when_single_proposal(self):
        result = synthesize(_verdict())
        assert result.alternatives_brief == ""

    def test_alternative_shows_best_runner_up_content(self):
        result = synthesize(_verdict(
            others=[
                (GeneratorRole.EXPLORADOR, 0.61, "usar shadow traffic"),
                (GeneratorRole.CRITICO, 0.45, "alto riesgo"),
            ],
        ))
        assert "explorador" in result.alternatives_brief
        assert "usar shadow traffic" in result.alternatives_brief

    def test_alternative_picks_highest_scoring_runner_up(self):
        result = synthesize(_verdict(
            others=[
                (GeneratorRole.CRITICO, 0.80, "opción segura"),
                (GeneratorRole.EXPLORADOR, 0.40, "opción creativa"),
            ],
        ))
        assert "opción segura" in result.alternatives_brief

    def test_alternative_empty_for_empty_content(self):
        result = synthesize(_verdict(
            others=[(GeneratorRole.EXPLORADOR, 0.5, "")],
        ))
        assert result.alternatives_brief == ""


class TestRenderSynthesis:
    def test_render_includes_approach(self):
        result = synthesize(_verdict(content="usar JWT"))
        rendered = render_synthesis(result)
        assert "Enfoque recomendado: usar JWT" in rendered

    def test_render_includes_risks(self):
        result = synthesize(_verdict(
            others=[(GeneratorRole.CRITICO, 0.5, "puede fallar bajo carga")],
        ))
        rendered = render_synthesis(result)
        assert "Riesgos identificados:" in rendered
        assert "puede fallar bajo carga" in rendered

    def test_render_includes_alternative(self):
        result = synthesize(_verdict(
            others=[(GeneratorRole.EXPLORADOR, 0.55, "probar canary deploy")],
        ))
        rendered = render_synthesis(result)
        assert "Alternativa considerada:" in rendered

    def test_render_includes_confidence(self):
        result = synthesize(_verdict())
        rendered = render_synthesis(result)
        assert "Confianza:" in rendered

    def test_render_includes_doubt_note(self):
        result = synthesize(_verdict(under_doubt=True))
        rendered = render_synthesis(result)
        assert "no alcanzó plena confianza" in rendered

    def test_render_no_doubt_note_when_accepted(self):
        result = synthesize(_verdict(under_doubt=False))
        rendered = render_synthesis(result)
        assert "no alcanzó" not in rendered

    def test_render_omits_empty_alternatives(self):
        result = synthesize(_verdict())
        rendered = render_synthesis(result)
        assert "Alternativa considerada:" not in rendered
