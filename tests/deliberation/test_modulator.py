"""Tests for posture modulation of deliberation prompt."""

from durin.deliberation.modulator import posture_modulation


class TestPostureModulation:
    def test_empty_posture(self):
        assert posture_modulation({}) == ""

    def test_high_cautela(self):
        result = posture_modulation({"caution": 0.8})
        assert "exhaustive" in result

    def test_low_cautela(self):
        result = posture_modulation({"caution": 0.3})
        assert "brief" in result

    def test_high_exploracion(self):
        result = posture_modulation({"exploration": 0.7})
        assert "radically" in result

    def test_high_profundidad(self):
        result = posture_modulation({"depth": 0.8})
        assert "detailed" in result

    def test_low_profundidad(self):
        result = posture_modulation({"depth": 0.4})
        assert "concise" in result
