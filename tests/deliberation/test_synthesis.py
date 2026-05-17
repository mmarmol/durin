"""Tests for deliberation synthesis rendering."""

from durin.deliberation.synthesis import render_for_injection
from durin.deliberation.types import DeliberationResult, Perspective


class TestRenderForInjection:
    def test_renders_all_perspectives(self):
        result = DeliberationResult(
            perspectives=(
                Perspective(role="critic", content="Risk A"),
                Perspective(role="explorer", content="Alt B"),
                Perspective(role="pragmatic", content="Path C"),
            ),
            synthesis="Do C with caution from A.",
        )
        rendered = render_for_injection(result)
        assert "[Pre-analysis deliberation]" in rendered
        assert "Risks identified: Risk A" in rendered
        assert "Alternative considered: Alt B" in rendered
        assert "Direct approach: Path C" in rendered
        assert "Synthesis: Do C with caution from A." in rendered

    def test_unknown_role_uses_capitalized(self):
        result = DeliberationResult(
            perspectives=(Perspective(role="custom", content="Custom view"),),
            synthesis="Summary.",
        )
        rendered = render_for_injection(result)
        assert "Custom: Custom view" in rendered

    def test_empty_synthesis_omitted(self):
        result = DeliberationResult(
            perspectives=(Perspective(role="critic", content="Issue"),),
            synthesis="",
        )
        rendered = render_for_injection(result)
        assert "Synthesis" not in rendered
