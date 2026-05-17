"""Tests for posture modulation of deliberation prompt."""

from durin.deliberation.modulator import posture_modulation


class TestPostureModulation:
    def test_empty_posture(self):
        assert posture_modulation({}) == ""

    def test_high_cautela(self):
        result = posture_modulation({"cautela": 0.8})
        assert "exhaustivo" in result

    def test_low_cautela(self):
        result = posture_modulation({"cautela": 0.3})
        assert "breve" in result

    def test_high_exploracion(self):
        result = posture_modulation({"exploracion": 0.7})
        assert "radicalmente" in result

    def test_high_profundidad(self):
        result = posture_modulation({"profundidad": 0.8})
        assert "detallada" in result

    def test_low_profundidad(self):
        result = posture_modulation({"profundidad": 0.4})
        assert "concisa" in result
