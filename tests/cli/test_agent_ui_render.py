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
                "cautela": 0.6,
                "exploracion": 0.4,
                "profundidad": 0.5,
                "disciplina": 0.5,
                "conformidad": 0.7,
            },
            "deltas": {},
        }
        render_posture_update(c, data)
        output = c.file.getvalue()
        assert "Cautela" in output
        assert "Exploración" in output
        assert "Profundidad" in output
        assert "60%" in output
        assert "█" in output

    def test_renders_deltas(self):
        c = _console()
        data = {
            "axes": {"cautela": 0.65, "exploracion": 0.35},
            "deltas": {"cautela": 0.05, "exploracion": -0.05},
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
            "winner": {
                "role": "pragmatico",
                "content": "usar JWT para autenticación",
                "score": 0.75,
            },
            "proposals": [
                {"role": "pragmatico", "content": "usar JWT", "score": 0.75},
                {"role": "explorador", "content": "probar passkeys", "score": 0.6},
                {"role": "critico", "content": "OAuth2 estándar", "score": 0.7},
            ],
            "threshold": 0.55,
            "rounds_used": 1,
            "under_doubt": False,
            "accepted": True,
        }
        render_deliberation_result(c, data)
        output = c.file.getvalue()
        assert "Pragmático" in output
        assert "7.5/10" in output
        assert "Explorador" in output
        assert "Crítico" in output

    def test_under_doubt_shows_warning(self):
        c = _console()
        data = {
            "winner": {"role": "critico", "content": "safe path", "score": 0.45},
            "proposals": [
                {"role": "critico", "content": "safe path", "score": 0.45},
            ],
            "threshold": 0.55,
            "rounds_used": 3,
            "under_doubt": True,
            "accepted": True,
        }
        render_deliberation_result(c, data)
        output = c.file.getvalue()
        assert "bajo duda" in output

    def test_no_winner_noop(self):
        c = _console()
        render_deliberation_result(c, {"winner": None, "proposals": []})
        assert c.file.getvalue() == ""


class TestRenderAgentUI:
    def test_dispatches_posture(self):
        c = _console()
        blob = {
            "kind": "posture_update",
            "data": {"axes": {"cautela": 0.6}, "deltas": {}},
        }
        assert render_agent_ui(c, blob) is True
        assert "Cautela" in c.file.getvalue()

    def test_dispatches_deliberation(self):
        c = _console()
        blob = {
            "kind": "deliberation_result",
            "data": {
                "winner": {"role": "explorador", "content": "try X", "score": 0.7},
                "proposals": [
                    {"role": "explorador", "content": "try X", "score": 0.7},
                ],
                "threshold": 0.5,
                "rounds_used": 1,
                "under_doubt": False,
                "accepted": True,
            },
        }
        assert render_agent_ui(c, blob) is True
        assert "Explorador" in c.file.getvalue()

    def test_unknown_kind_returns_false(self):
        c = _console()
        assert render_agent_ui(c, {"kind": "unknown", "data": {}}) is False

    def test_missing_data_returns_false(self):
        c = _console()
        assert render_agent_ui(c, {"kind": "posture_update"}) is False
