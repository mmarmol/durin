"""Tests for homeostasis update functions."""

from __future__ import annotations

import pytest

from durin.posture.homeostasis import (
    apply_clamp,
    apply_return_to_mean,
    apply_stimulus,
    update_axis,
    update_vector,
)
from durin.posture.vector import AxisName, AxisState, PostureVector


def _make_state(
    media: float = 0.5,
    varianza: float = 0.15,
    fuerza_retorno: float = 0.3,
    valor_actual: float = 0.5,
) -> AxisState:
    return AxisState(
        media=media, varianza=varianza,
        fuerza_retorno=fuerza_retorno, valor_actual=valor_actual,
    )


class TestApplyReturnToMean:
    def test_moves_toward_media(self):
        state = _make_state(media=0.6, valor_actual=0.4, fuerza_retorno=0.3)
        result = apply_return_to_mean(state)
        assert result.valor_actual == pytest.approx(0.4 + 0.3 * (0.6 - 0.4))

    def test_at_media_stays_unchanged(self):
        state = _make_state(media=0.5, valor_actual=0.5, fuerza_retorno=0.3)
        result = apply_return_to_mean(state)
        assert result.valor_actual == pytest.approx(0.5)

    def test_above_media_decreases(self):
        state = _make_state(media=0.5, valor_actual=0.8, fuerza_retorno=0.3)
        result = apply_return_to_mean(state)
        assert result.valor_actual < 0.8

    def test_zero_fuerza_retorno_no_drift(self):
        state = _make_state(media=0.5, valor_actual=0.9, fuerza_retorno=0.0)
        result = apply_return_to_mean(state)
        assert result.valor_actual == pytest.approx(0.9)

    def test_full_fuerza_retorno_snaps_to_media(self):
        state = _make_state(media=0.5, valor_actual=0.9, fuerza_retorno=1.0)
        result = apply_return_to_mean(state)
        assert result.valor_actual == pytest.approx(0.5)

    def test_preserves_other_fields(self):
        state = _make_state(media=0.6, varianza=0.2, fuerza_retorno=0.3, valor_actual=0.4)
        result = apply_return_to_mean(state)
        assert result.media == 0.6
        assert result.varianza == 0.2
        assert result.fuerza_retorno == 0.3


class TestApplyStimulus:
    def test_positive_delta_increases_value(self):
        state = _make_state(valor_actual=0.5, varianza=0.15)
        result = apply_stimulus(state, 0.10)
        assert result.valor_actual > 0.5

    def test_negative_delta_decreases_value(self):
        state = _make_state(valor_actual=0.5, varianza=0.15)
        result = apply_stimulus(state, -0.05)
        assert result.valor_actual < 0.5

    def test_reference_varianza_gives_raw_delta(self):
        state = _make_state(valor_actual=0.5, varianza=0.15)
        result = apply_stimulus(state, 0.10)
        assert result.valor_actual == pytest.approx(0.5 + 0.10)

    def test_larger_varianza_amplifies_delta(self):
        state = _make_state(valor_actual=0.5, varianza=0.20)
        result = apply_stimulus(state, 0.10)
        expected = 0.5 + 0.10 * (0.20 / 0.15)
        assert result.valor_actual == pytest.approx(expected)

    def test_smaller_varianza_attenuates_delta(self):
        state = _make_state(valor_actual=0.5, varianza=0.10)
        result = apply_stimulus(state, 0.10)
        expected = 0.5 + 0.10 * (0.10 / 0.15)
        assert result.valor_actual == pytest.approx(expected)

    def test_zero_delta_no_change(self):
        state = _make_state(valor_actual=0.5)
        result = apply_stimulus(state, 0.0)
        assert result.valor_actual == pytest.approx(0.5)


class TestApplyClamp:
    def test_within_bounds_unchanged(self):
        state = _make_state(media=0.5, varianza=0.15, valor_actual=0.5)
        result = apply_clamp(state)
        assert result.valor_actual == pytest.approx(0.5)

    def test_above_upper_bound_clamped(self):
        state = _make_state(media=0.5, varianza=0.15, valor_actual=0.95)
        result = apply_clamp(state)
        upper = min(1.0, 0.5 + 2 * 0.15)
        assert result.valor_actual == pytest.approx(upper)

    def test_below_lower_bound_clamped(self):
        state = _make_state(media=0.5, varianza=0.15, valor_actual=0.05)
        result = apply_clamp(state)
        lower = max(0.0, 0.5 - 2 * 0.15)
        assert result.valor_actual == pytest.approx(lower)

    def test_lower_bound_respects_zero_floor(self):
        state = _make_state(media=0.1, varianza=0.15, valor_actual=0.0)
        result = apply_clamp(state)
        assert result.valor_actual >= 0.0

    def test_upper_bound_respects_one_ceiling(self):
        state = _make_state(media=0.9, varianza=0.15, valor_actual=1.0)
        result = apply_clamp(state)
        assert result.valor_actual <= 1.0

    def test_at_exact_boundary_unchanged(self):
        state = _make_state(media=0.5, varianza=0.15, valor_actual=0.8)
        result = apply_clamp(state)
        assert result.valor_actual == pytest.approx(0.8)


class TestUpdateAxis:
    def test_order_is_return_then_stimulus_then_clamp(self):
        state = _make_state(media=0.5, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.8)
        result = update_axis(state, delta=0.10)

        after_return = 0.8 + 0.3 * (0.5 - 0.8)
        after_stimulus = after_return + 0.10 * (0.15 / 0.15)
        upper = min(1.0, 0.5 + 2 * 0.15)
        expected = min(upper, max(max(0.0, 0.5 - 2 * 0.15), after_stimulus))

        assert result.valor_actual == pytest.approx(expected)

    def test_with_zero_delta_only_drifts(self):
        state = _make_state(media=0.5, valor_actual=0.8, fuerza_retorno=0.3)
        result = update_axis(state, delta=0.0)
        assert result.valor_actual < 0.8
        assert result.valor_actual > 0.5

    def test_large_delta_gets_clamped(self):
        state = _make_state(media=0.5, varianza=0.15, valor_actual=0.5)
        result = update_axis(state, delta=1.0)
        upper = 0.5 + 2 * 0.15
        assert result.valor_actual == pytest.approx(upper)


class TestUpdateVector:
    def test_applies_deltas_to_specified_axes(self):
        v = PostureVector.default()
        result = update_vector(v, {AxisName.CAUTELA: 0.10})

        assert result.axes[AxisName.CAUTELA].valor_actual != v.axes[AxisName.CAUTELA].valor_actual

    def test_unspecified_axes_still_drift_toward_media(self):
        v = PostureVector.default()
        cautela = v.axes[AxisName.CAUTELA]
        shifted = cautela.model_copy(update={"valor_actual": 0.9})
        v = v.with_update({AxisName.CAUTELA: shifted})

        result = update_vector(v, {})
        assert result.axes[AxisName.CAUTELA].valor_actual < 0.9

    def test_returns_new_vector_instance(self):
        v = PostureVector.default()
        result = update_vector(v, {AxisName.CAUTELA: 0.10})
        assert result is not v

    def test_preserves_all_five_axes(self):
        v = PostureVector.default()
        result = update_vector(v, {AxisName.CAUTELA: 0.05})
        assert set(result.axes.keys()) == set(AxisName)

    def test_empty_deltas_all_axes_drift(self):
        axes = {}
        for name in AxisName:
            axes[name] = AxisState(
                media=0.5, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.8,
            )
        v = PostureVector(axes=axes)
        result = update_vector(v, {})

        for name in AxisName:
            assert result.axes[name].valor_actual < 0.8

    def test_multiple_deltas_applied_independently(self):
        v = PostureVector.default()
        result = update_vector(v, {
            AxisName.CAUTELA: 0.10,
            AxisName.EXPLORACION: -0.05,
        })

        v_cautela_only = update_vector(v, {AxisName.CAUTELA: 0.10})
        v_exp_only = update_vector(v, {AxisName.EXPLORACION: -0.05})

        assert result.axes[AxisName.CAUTELA].valor_actual == pytest.approx(
            v_cautela_only.axes[AxisName.CAUTELA].valor_actual,
        )
        assert result.axes[AxisName.EXPLORACION].valor_actual == pytest.approx(
            v_exp_only.axes[AxisName.EXPLORACION].valor_actual,
        )
