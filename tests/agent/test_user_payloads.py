"""Tests for durin.agent.user_payloads — channel capability + fallback serialization."""

from durin.agent.user_payloads import (
    channel_renders_tool_payloads,
    serialize_pending_interactions,
)


def test_rich_channels_render_payloads():
    assert channel_renders_tool_payloads("websocket") is True
    assert channel_renders_tool_payloads("cli") is True


def test_dumb_channels_do_not_render_payloads():
    for name in ("telegram", "email", "slack", "whatsapp", ""):
        assert channel_renders_tool_payloads(name) is False
    assert channel_renders_tool_payloads(None) is False


def test_serialize_pending_question_with_options():
    meta = {
        "pending_question": {
            "question_id": "abc123",
            "question": "Which color?",
            "options": ["red", "green"],
        }
    }
    out = serialize_pending_interactions(meta)
    assert len(out) == 1
    assert "Which color?" in out[0]
    assert "1. red" in out[0]
    assert "2. green" in out[0]


def test_serialize_pending_question_without_options():
    meta = {"pending_question": {"question_id": "x", "question": "Why?", "options": []}}
    out = serialize_pending_interactions(meta)
    assert len(out) == 1
    assert "Why?" in out[0]
    assert "1." not in out[0]


def test_serialize_pending_secret_request():
    meta = {
        "pending_secret_request": {
            "name": "GH_TOKEN",
            "service": "github",
            "purpose": "push commits",
        }
    }
    out = serialize_pending_interactions(meta)
    assert len(out) == 1
    assert "durin secret set GH_TOKEN --service github --scope exec" in out[0]
    assert "push commits" in out[0]


def test_serialize_pending_plan_review():
    meta = {
        "pending_plan_review": {
            "path": ".durin/plans/s/plan_1.md",
            "plan": "# Goal\n\n1. step one\n2. step two",
        }
    }
    out = serialize_pending_interactions(meta)
    assert len(out) == 1
    assert "# Goal" in out[0]
    assert "/build" in out[0]


def test_serialize_long_plan_truncates():
    meta = {
        "pending_plan_review": {
            "path": "p.md",
            "plan": "x" * 10_000,
        }
    }
    out = serialize_pending_interactions(meta)
    assert len(out[0]) < 5_000
    assert "p.md" in out[0]


def test_serialize_empty_metadata():
    assert serialize_pending_interactions({}) == []
    assert serialize_pending_interactions(None) == []
