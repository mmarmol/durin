"""Tests for PostureVector data model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from durin.posture.vector import AxisName, AxisState, PostureVector


class TestAxisState:
    def test_valid_construction(self):
        state = AxisState(media=0.5, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.6)
        assert state.media == 0.5
        assert state.valor_actual == 0.6

    def test_rejects_media_above_one(self):
        with pytest.raises(ValidationError):
            AxisState(media=1.1, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.5)

    def test_rejects_negative_media(self):
        with pytest.raises(ValidationError):
            AxisState(media=-0.1, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.5)

    def test_rejects_zero_varianza(self):
        with pytest.raises(ValidationError):
            AxisState(media=0.5, varianza=0.0, fuerza_retorno=0.3, valor_actual=0.5)

    def test_rejects_varianza_above_half(self):
        with pytest.raises(ValidationError):
            AxisState(media=0.5, varianza=0.51, fuerza_retorno=0.3, valor_actual=0.5)

    def test_rejects_negative_valor_actual(self):
        with pytest.raises(ValidationError):
            AxisState(media=0.5, varianza=0.15, fuerza_retorno=0.3, valor_actual=-0.01)

    def test_rejects_valor_actual_above_one(self):
        with pytest.raises(ValidationError):
            AxisState(media=0.5, varianza=0.15, fuerza_retorno=0.3, valor_actual=1.01)

    def test_allows_zero_fuerza_retorno(self):
        state = AxisState(media=0.5, varianza=0.15, fuerza_retorno=0.0, valor_actual=0.5)
        assert state.fuerza_retorno == 0.0

    def test_is_immutable(self):
        state = AxisState(media=0.5, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.5)
        with pytest.raises(ValidationError):
            state.valor_actual = 0.9  # type: ignore[misc]

    def test_boundary_values_accepted(self):
        AxisState(media=0.0, varianza=0.01, fuerza_retorno=0.0, valor_actual=0.0)
        AxisState(media=1.0, varianza=0.5, fuerza_retorno=1.0, valor_actual=1.0)


class TestAxisName:
    def test_all_five_axes_exist(self):
        names = set(AxisName)
        assert len(names) == 5
        assert AxisName.CAUTELA in names
        assert AxisName.EXPLORACION in names
        assert AxisName.PROFUNDIDAD in names
        assert AxisName.DISCIPLINA in names
        assert AxisName.CONFORMIDAD in names

    def test_values_are_lowercase_strings(self):
        for name in AxisName:
            assert name.value == name.value.lower()


class TestPostureVector:
    def test_default_has_five_axes(self):
        v = PostureVector.default()
        assert len(v.axes) == 5
        assert set(v.axes.keys()) == set(AxisName)

    def test_default_values_match_spec(self):
        v = PostureVector.default()
        assert v.axes[AxisName.CAUTELA].media == 0.6
        assert v.axes[AxisName.CAUTELA].varianza == 0.15
        assert v.axes[AxisName.CAUTELA].fuerza_retorno == 0.3
        assert v.axes[AxisName.EXPLORACION].media == 0.4
        assert v.axes[AxisName.EXPLORACION].varianza == 0.20
        assert v.axes[AxisName.PROFUNDIDAD].fuerza_retorno == 0.5
        assert v.axes[AxisName.CONFORMIDAD].media == 0.7

    def test_default_valor_actual_equals_media(self):
        v = PostureVector.default()
        for state in v.axes.values():
            assert state.valor_actual == state.media

    def test_snapshot_returns_current_values(self):
        v = PostureVector.default()
        snap = v.snapshot()
        assert snap[AxisName.CAUTELA] == 0.6
        assert snap[AxisName.EXPLORACION] == 0.4

    def test_with_update_returns_new_instance(self):
        v = PostureVector.default()
        new_state = AxisState(media=0.6, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.9)
        v2 = v.with_update({AxisName.CAUTELA: new_state})

        assert v2 is not v
        assert v2.axes[AxisName.CAUTELA].valor_actual == 0.9
        assert v.axes[AxisName.CAUTELA].valor_actual == 0.6  # original unchanged

    def test_with_update_preserves_other_axes(self):
        v = PostureVector.default()
        new_state = AxisState(media=0.6, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.9)
        v2 = v.with_update({AxisName.CAUTELA: new_state})

        assert v2.axes[AxisName.EXPLORACION] == v.axes[AxisName.EXPLORACION]
        assert v2.axes[AxisName.PROFUNDIDAD] == v.axes[AxisName.PROFUNDIDAD]

    def test_is_immutable(self):
        v = PostureVector.default()
        with pytest.raises(ValidationError):
            v.axes = {}  # type: ignore[misc]
