"""Tests for posture phrase generation."""

from __future__ import annotations

from durin.posture.phrase import generate_posture_phrase
from durin.posture.vector import AxisName, AxisState, PostureVector


def _make_vector(values: dict[AxisName, float]) -> PostureVector:
    axes = {}
    for name in AxisName:
        v = values.get(name, 0.5)
        axes[name] = AxisState(mean=0.5, variance=0.2, return_force=0.3, current_value=v)
    return PostureVector(axes=axes)


class TestGeneratePosturePhrase:
    def test_all_high_produces_all_phrases(self):
        v = _make_vector({name: 0.9 for name in AxisName})
        phrase = generate_posture_phrase(v)
        assert phrase.startswith("Current posture:")
        assert "reversibility" in phrase.lower()
        assert "alternatives" in phrase.lower()
        assert "depth" in phrase.lower()
        assert "protocol" in phrase.lower()
        assert "deviation" in phrase.lower()

    def test_all_low_produces_all_low_phrases(self):
        v = _make_vector({name: 0.1 for name in AxisName})
        phrase = generate_posture_phrase(v)
        assert phrase.startswith("Current posture:")
        assert "risk" in phrase.lower()
        assert "known" in phrase.lower()
        assert "direct" in phrase.lower()
        assert "Adapt" in phrase
        assert "Question" in phrase

    def test_all_mid_returns_empty(self):
        v = _make_vector({name: 0.5 for name in AxisName})
        phrase = generate_posture_phrase(v)
        assert phrase == ""

    def test_mixed_omits_mid_axes(self):
        v = _make_vector({
            AxisName.CAUTION: 0.9,
            AxisName.EXPLORATION: 0.5,
            AxisName.DEPTH: 0.5,
            AxisName.DISCIPLINE: 0.5,
            AxisName.CONFORMITY: 0.5,
        })
        phrase = generate_posture_phrase(v)
        assert "reversibility" in phrase.lower()
        assert "alternatives" not in phrase.lower()
        assert "protocol" not in phrase.lower()

    def test_boundary_0_3_is_mid(self):
        v = _make_vector({AxisName.CAUTION: 0.3})
        phrase = generate_posture_phrase(v)
        assert "risk" not in phrase.lower()

    def test_boundary_below_0_3_is_low(self):
        v = _make_vector({AxisName.CAUTION: 0.29})
        phrase = generate_posture_phrase(v)
        assert "risk" in phrase.lower()

    def test_boundary_0_7_is_high(self):
        v = _make_vector({AxisName.CAUTION: 0.7})
        phrase = generate_posture_phrase(v)
        assert "reversibility" in phrase.lower()

    def test_boundary_below_0_7_is_mid(self):
        v = _make_vector({AxisName.CAUTION: 0.69})
        phrase = generate_posture_phrase(v)
        assert "reversibility" not in phrase.lower()
        assert "risk" not in phrase.lower()

    def test_default_vector_produces_phrase(self):
        v = PostureVector.default()
        phrase = generate_posture_phrase(v)
        assert "Execute" in phrase

    def test_phrase_length_reasonable(self):
        v = _make_vector({name: 0.9 for name in AxisName})
        phrase = generate_posture_phrase(v)
        assert len(phrase) < 300
