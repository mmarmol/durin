"""Tests for deliberation V3 types."""

from durin.deliberation.types import (
    DeliberationContext,
    DeliberationResult,
    HistoryEntry,
    Perspective,
)


class TestPerspective:
    def test_frozen(self):
        p = Perspective(role="critic", content="risk here")
        assert p.role == "critic"
        assert p.content == "risk here"


class TestDeliberationResult:
    def test_frozen(self):
        r = DeliberationResult(
            perspectives=(Perspective(role="critic", content="x"),),
            synthesis="do y",
            duration_ms=123.4,
            model="glm-5.1",
        )
        assert r.model == "glm-5.1"
        assert r.duration_ms == 123.4


class TestDeliberationContext:
    def test_defaults(self):
        ctx = DeliberationContext(
            goal_summary="fix bug",
            investigation_context="code here",
        )
        assert ctx.posture_snapshot == {}
        assert ctx.previous_failure == ""

    def test_with_failure(self):
        ctx = DeliberationContext(
            goal_summary="fix",
            investigation_context="ctx",
            previous_failure="tests broke",
        )
        assert ctx.previous_failure == "tests broke"


class TestHistoryEntry:
    def test_fields(self):
        e = HistoryEntry(
            timestamp=1.0,
            trigger="investigate_to_plan",
            synthesis_brief="do x",
            perspectives_count=3,
            duration_ms=500.0,
            cycle=1,
        )
        assert e.trigger == "investigate_to_plan"
        assert e.perspectives_count == 3
