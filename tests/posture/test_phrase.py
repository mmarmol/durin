"""Tests for posture phrase generation."""

from __future__ import annotations

from durin.posture.phrase import generate_posture_phrase
from durin.posture.vector import AxisName, AxisState, PostureVector


def _make_vector(values: dict[AxisName, float]) -> PostureVector:
    axes = {}
    for name in AxisName:
        v = values.get(name, 0.5)
        axes[name] = AxisState(media=0.5, varianza=0.2, fuerza_retorno=0.3, valor_actual=v)
    return PostureVector(axes=axes)


class TestGeneratePosturePhrase:
    def test_all_high_produces_all_phrases(self):
        v = _make_vector({name: 0.9 for name in AxisName})
        phrase = generate_posture_phrase(v)
        assert phrase.startswith("Postura actual:")
        assert "reversibilidad" in phrase
        assert "alternativas" in phrase
        assert "profundidad" in phrase.lower() or "Deliberá" in phrase
        assert "protocolo" in phrase
        assert "Ejecutá" in phrase

    def test_all_low_produces_all_low_phrases(self):
        v = _make_vector({name: 0.1 for name in AxisName})
        phrase = generate_posture_phrase(v)
        assert phrase.startswith("Postura actual:")
        assert "riesgo" in phrase
        assert "conocido" in phrase
        assert "directo" in phrase
        assert "Adaptá" in phrase
        assert "Cuestioná" in phrase

    def test_all_mid_returns_empty(self):
        v = _make_vector({name: 0.5 for name in AxisName})
        phrase = generate_posture_phrase(v)
        assert phrase == ""

    def test_mixed_omits_mid_axes(self):
        v = _make_vector({
            AxisName.CAUTELA: 0.9,
            AxisName.EXPLORACION: 0.5,
            AxisName.PROFUNDIDAD: 0.5,
            AxisName.DISCIPLINA: 0.5,
            AxisName.CONFORMIDAD: 0.5,
        })
        phrase = generate_posture_phrase(v)
        assert "reversibilidad" in phrase
        assert "alternativas" not in phrase
        assert "protocolo" not in phrase

    def test_boundary_0_3_is_mid(self):
        v = _make_vector({AxisName.CAUTELA: 0.3})
        phrase = generate_posture_phrase(v)
        assert "riesgo" not in phrase

    def test_boundary_below_0_3_is_low(self):
        v = _make_vector({AxisName.CAUTELA: 0.29})
        phrase = generate_posture_phrase(v)
        assert "riesgo" in phrase

    def test_boundary_0_7_is_high(self):
        v = _make_vector({AxisName.CAUTELA: 0.7})
        phrase = generate_posture_phrase(v)
        assert "reversibilidad" in phrase

    def test_boundary_below_0_7_is_mid(self):
        v = _make_vector({AxisName.CAUTELA: 0.69})
        phrase = generate_posture_phrase(v)
        assert "reversibilidad" not in phrase
        assert "riesgo" not in phrase

    def test_default_vector_produces_phrase(self):
        v = PostureVector.default()
        phrase = generate_posture_phrase(v)
        # Default: Cautela 0.6 (MID), Exploracion 0.4 (MID), Profundidad 0.5 (MID),
        # Disciplina 0.5 (MID), Conformidad 0.7 (HIGH)
        assert "Ejecutá" in phrase

    def test_phrase_length_reasonable(self):
        v = _make_vector({name: 0.9 for name in AxisName})
        phrase = generate_posture_phrase(v)
        assert len(phrase) < 300
