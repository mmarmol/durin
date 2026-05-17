"""Tests for CLI agent UI rendering (posture, deliberation)."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from durin.cli.agent_ui_render import (
    render_agent_ui,
    render_deliberation_result,
    render_posture_update,
)


def _console() -> Console:
    return Console(file=StringIO(), width=80, force_terminal=True)


class TestRenderPostureUpdate:
    def test_renders_all_axes(self):
        c = _console()
        data = {
            "axes": {
                "caution": 0.6,
                "exploration": 0.4,
                "depth": 0.5,
                "discipline": 0.5,
                "conformity": 0.7,
            },
            "deltas": {},
        }
        render_posture_update(c, data)
        output = c.file.getvalue()
        assert "Caution" in output
        assert "Exploration" in output
        assert "Depth" in output
        assert "60%" in output
        assert "█" in output

    def test_renders_deltas(self):
        c = _console()
        data = {
            "axes": {"caution": 0.65, "exploration": 0.35},
            "deltas": {"caution": 0.05, "exploration": -0.05},
        }
        render_posture_update(c, data)
        output = c.file.getvalue()
        assert "+5.0%" in output
        assert "-5.0%" in output

    def test_empty_axes_noop(self):
        c = _console()
        render_posture_update(c, {"axes": {}, "deltas": {}})
        assert c.file.getvalue() == ""


class TestRenderDeliberationResult:
    def test_renders_winner(self):
        c = _console()
        data = {
            "perspectives": {
                "pragmatic": "Use JWT for authentication",
                "explorer": "Try passkeys",
                "critic": "OAuth2 standard",
            },
            "synthesis": "Use JWT with fallback.",
            "duration_ms": 1500,
        }
        render_deliberation_result(c, data)
        output = c.file.getvalue()
        assert "Pragmatic" in output
        assert "Explorer" in output
        assert "Critic" in output
        assert "Synthesis" in output

    def test_empty_perspectives_noop(self):
        c = _console()
        data = {
            "perspectives": {},
            "synthesis": "",
            "duration_ms": 0,
        }
        render_deliberation_result(c, data)
        assert c.file.getvalue() == ""

    def test_no_winner_noop(self):
        c = _console()
        render_deliberation_result(c, {"winner": None, "proposals": []})
        assert c.file.getvalue() == ""


class TestRenderAgentUI:
    def test_dispatches_posture(self):
        c = _console()
        blob = {
            "kind": "posture_update",
            "data": {"axes": {"caution": 0.6}, "deltas": {}},
        }
        assert render_agent_ui(c, blob) is True
        assert "Caution" in c.file.getvalue()

    def test_dispatches_deliberation(self):
        c = _console()
        blob = {
            "kind": "deliberation_result",
            "data": {
                "perspectives": {"explorer": "try X"},
                "synthesis": "do X",
                "duration_ms": 100,
            },
        }
        assert render_agent_ui(c, blob) is True
        assert "Explorer" in c.file.getvalue()

    def test_unknown_kind_returns_false(self):
        c = _console()
        assert render_agent_ui(c, {"kind": "unknown", "data": {}}) is False

    def test_missing_data_returns_false(self):
        c = _console()
        assert render_agent_ui(c, {"kind": "posture_update"}) is False
