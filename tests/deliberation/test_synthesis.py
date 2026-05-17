"""Tests for deliberation synthesis V2 — multi-perspective enrichment."""

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
    score: float = 0.5,
    threshold: float = 0.5,
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


class TestSynthesizeV2:
    def test_returns_synthesis_result(self):
        result = synthesize(_verdict())
        assert isinstance(result, SynthesisResult)

    def test_direction_from_pragmatico(self):
        v = _verdict(
            role=GeneratorRole.PRAGMATICO,
            content="  usar JWT tokens  ",
            others=[
                (GeneratorRole.EXPLORADOR, 0.5, "probar OAuth2"),
                (GeneratorRole.CRITICO, 0.5, "riesgo de leak"),
            ],
        )
        result = synthesize(v)
        assert result.direction == "usar JWT tokens"

    def test_reasoning_from_critico(self):
        v = _verdict(
            others=[
                (GeneratorRole.EXPLORADOR, 0.5, "alternativa"),
                (GeneratorRole.CRITICO, 0.5, "puede fallar bajo carga"),
            ],
        )
        result = synthesize(v)
        assert "puede fallar bajo carga" in result.reasoning

    def test_alternatives_from_explorador(self):
        v = _verdict(
            others=[
                (GeneratorRole.EXPLORADOR, 0.5, "usar canary deploy"),
                (GeneratorRole.CRITICO, 0.5, "riesgo"),
            ],
        )
        result = synthesize(v)
        assert "usar canary deploy" in result.alternatives_brief

    def test_confidence_always_alta_in_v2(self):
        result = synthesize(_verdict())
        assert result.confidence == "alta"

    def test_under_doubt_propagates(self):
        assert synthesize(_verdict(under_doubt=False)).under_doubt is False
        assert synthesize(_verdict(under_doubt=True)).under_doubt is True

    def test_empty_reasoning_when_no_critico(self):
        v = _verdict(others=[(GeneratorRole.EXPLORADOR, 0.5, "alt")])
        result = synthesize(v)
        assert result.reasoning == ""

    def test_empty_alternatives_when_no_explorador(self):
        v = _verdict(others=[(GeneratorRole.CRITICO, 0.5, "risk")])
        result = synthesize(v)
        assert result.alternatives_brief == ""


class TestRenderSynthesisV2:
    def test_render_includes_perspectiva_directa(self):
        v = _verdict(
            content="usar JWT",
            others=[
                (GeneratorRole.EXPLORADOR, 0.5, "probar OAuth"),
                (GeneratorRole.CRITICO, 0.5, "riesgo"),
            ],
        )
        rendered = render_synthesis(synthesize(v))
        assert "Perspectiva directa: usar JWT" in rendered

    def test_render_includes_perspectiva_alternativa(self):
        v = _verdict(
            others=[
                (GeneratorRole.EXPLORADOR, 0.5, "probar canary"),
                (GeneratorRole.CRITICO, 0.5, "risk"),
            ],
        )
        rendered = render_synthesis(synthesize(v))
        assert "Perspectiva alternativa: probar canary" in rendered

    def test_render_includes_riesgos(self):
        v = _verdict(
            others=[
                (GeneratorRole.EXPLORADOR, 0.5, "alt"),
                (GeneratorRole.CRITICO, 0.5, "puede romper prod"),
            ],
        )
        rendered = render_synthesis(synthesize(v))
        assert "Riesgos a considerar: puede romper prod" in rendered

    def test_render_omits_empty_sections(self):
        v = _verdict()  # only pragmatico, no others
        rendered = render_synthesis(synthesize(v))
        assert "Perspectiva alternativa:" not in rendered
        assert "Riesgos a considerar:" not in rendered

    def test_render_multiline(self):
        v = _verdict(
            content="direct path",
            others=[
                (GeneratorRole.EXPLORADOR, 0.5, "explore"),
                (GeneratorRole.CRITICO, 0.5, "careful"),
            ],
        )
        rendered = render_synthesis(synthesize(v))
        lines = rendered.strip().split("\n")
        assert len(lines) == 3
