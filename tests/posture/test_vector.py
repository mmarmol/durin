"""Tests for PostureVector data model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from durin.posture.vector import AxisName, AxisState, PostureVector


class TestAxisState:
    def test_valid_construction(self):
        state = AxisState(mean=0.5, variance=0.15, return_force=0.3, current_value=0.6)
        assert state.mean == 0.5
        assert state.current_value == 0.6

    def test_rejects_media_above_one(self):
        with pytest.raises(ValidationError):
            AxisState(mean=1.1, variance=0.15, return_force=0.3, current_value=0.5)

    def test_rejects_negative_media(self):
        with pytest.raises(ValidationError):
            AxisState(mean=-0.1, variance=0.15, return_force=0.3, current_value=0.5)

    def test_rejects_zero_variance(self):
        with pytest.raises(ValidationError):
            AxisState(mean=0.5, variance=0.0, return_force=0.3, current_value=0.5)

    def test_rejects_variance_above_half(self):
        with pytest.raises(ValidationError):
            AxisState(mean=0.5, variance=0.51, return_force=0.3, current_value=0.5)

    def test_rejects_negative_current_value(self):
        with pytest.raises(ValidationError):
            AxisState(mean=0.5, variance=0.15, return_force=0.3, current_value=-0.01)

    def test_rejects_current_value_above_one(self):
        with pytest.raises(ValidationError):
            AxisState(mean=0.5, variance=0.15, return_force=0.3, current_value=1.01)

    def test_allows_zero_return_force(self):
        state = AxisState(mean=0.5, variance=0.15, return_force=0.0, current_value=0.5)
        assert state.return_force == 0.0

    def test_is_immutable(self):
        state = AxisState(mean=0.5, variance=0.15, return_force=0.3, current_value=0.5)
        with pytest.raises(ValidationError):
            state.current_value = 0.9  # type: ignore[misc]

    def test_boundary_values_accepted(self):
        AxisState(mean=0.0, variance=0.01, return_force=0.0, current_value=0.0)
        AxisState(mean=1.0, variance=0.5, return_force=1.0, current_value=1.0)


class TestAxisName:
    def test_all_five_axes_exist(self):
        names = set(AxisName)
        assert len(names) == 5
        assert AxisName.CAUTION in names
        assert AxisName.EXPLORATION in names
        assert AxisName.DEPTH in names
        assert AxisName.DISCIPLINE in names
        assert AxisName.CONFORMITY in names

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
        assert v.axes[AxisName.CAUTION].mean == 0.6
        assert v.axes[AxisName.CAUTION].variance == 0.15
        assert v.axes[AxisName.CAUTION].return_force == 0.3
        assert v.axes[AxisName.EXPLORATION].mean == 0.4
        assert v.axes[AxisName.EXPLORATION].variance == 0.20
        assert v.axes[AxisName.DEPTH].return_force == 0.5
        assert v.axes[AxisName.CONFORMITY].mean == 0.7

    def test_default_current_value_equals_media(self):
        v = PostureVector.default()
        for state in v.axes.values():
            assert state.current_value == state.mean

    def test_snapshot_returns_current_values(self):
        v = PostureVector.default()
        snap = v.snapshot()
        assert snap[AxisName.CAUTION] == 0.6
        assert snap[AxisName.EXPLORATION] == 0.4

    def test_with_update_returns_new_instance(self):
        v = PostureVector.default()
        new_state = AxisState(mean=0.6, variance=0.15, return_force=0.3, current_value=0.9)
        v2 = v.with_update({AxisName.CAUTION: new_state})

        assert v2 is not v
        assert v2.axes[AxisName.CAUTION].current_value == 0.9
        assert v.axes[AxisName.CAUTION].current_value == 0.6  # original unchanged

    def test_with_update_preserves_other_axes(self):
        v = PostureVector.default()
        new_state = AxisState(mean=0.6, variance=0.15, return_force=0.3, current_value=0.9)
        v2 = v.with_update({AxisName.CAUTION: new_state})

        assert v2.axes[AxisName.EXPLORATION] == v.axes[AxisName.EXPLORATION]
        assert v2.axes[AxisName.DEPTH] == v.axes[AxisName.DEPTH]

    def test_is_immutable(self):
        v = PostureVector.default()
        with pytest.raises(ValidationError):
            v.axes = {}  # type: ignore[misc]
