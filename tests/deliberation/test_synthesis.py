"""Tests for deliberation synthesis rendering."""

from durin.deliberation.synthesis import render_for_injection
from durin.deliberation.types import DeliberationResult, Perspective


class TestRenderForInjection:
    def test_renders_all_perspectives(self):
        result = DeliberationResult(
            perspectives=(
                Perspective(role="critico", content="Risk A"),
                Perspective(role="explorador", content="Alt B"),
                Perspective(role="pragmatico", content="Path C"),
            ),
            synthesis="Do C with caution from A.",
        )
        rendered = render_for_injection(result)
        assert "[Deliberación pre-análisis]" in rendered
        assert "Riesgos identificados: Risk A" in rendered
        assert "Alternativa considerada: Alt B" in rendered
        assert "Enfoque directo: Path C" in rendered
        assert "Síntesis: Do C with caution from A." in rendered

    def test_unknown_role_uses_capitalized(self):
        result = DeliberationResult(
            perspectives=(Perspective(role="custom", content="Custom view"),),
            synthesis="Summary.",
        )
        rendered = render_for_injection(result)
        assert "Custom: Custom view" in rendered

    def test_empty_synthesis_omitted(self):
        result = DeliberationResult(
            perspectives=(Perspective(role="critico", content="Issue"),),
            synthesis="",
        )
        rendered = render_for_injection(result)
        assert "Síntesis" not in rendered
