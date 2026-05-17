"""Tests for posture vector persistence and time decay."""

from __future__ import annotations

import math
import time
from unittest.mock import patch

import pytest

from durin.posture.persistence import (
    POSTURE_METADATA_KEY,
    apply_time_decay,
    deserialize,
    restore_posture,
    save_posture,
    serialize,
)
from durin.posture.vector import AxisName, AxisState, PostureVector


def _make_shifted_vector(shift: float = 0.2) -> PostureVector:
    """Create a vector where all axes have valor_actual shifted from media."""
    default = PostureVector.default()
    updates = {}
    for name, state in default.axes.items():
        new_val = min(1.0, state.media + shift)
        updates[name] = state.model_copy(update={"valor_actual": new_val})
    return default.with_update(updates)


class TestSerializeDeserialize:
    def test_roundtrip(self):
        v = PostureVector.default()
        data = serialize(v)
        restored = deserialize(data)
        for name in AxisName:
            assert restored.axes[name].valor_actual == pytest.approx(v.axes[name].valor_actual)
            assert restored.axes[name].media == v.axes[name].media
            assert restored.axes[name].varianza == v.axes[name].varianza
            assert restored.axes[name].fuerza_retorno == v.axes[name].fuerza_retorno

    def test_serialize_includes_timestamp(self):
        v = PostureVector.default()
        data = serialize(v)
        assert "timestamp" in data
        assert isinstance(data["timestamp"], float)

    def test_serialize_includes_all_axes(self):
        v = PostureVector.default()
        data = serialize(v)
        assert set(data["axes"].keys()) == {name.value for name in AxisName}

    def test_roundtrip_shifted_vector(self):
        v = _make_shifted_vector(0.15)
        data = serialize(v)
        restored = deserialize(data)
        for name in AxisName:
            assert restored.axes[name].valor_actual == pytest.approx(v.axes[name].valor_actual)


class TestApplyTimeDecay:
    def test_zero_elapsed_unchanged(self):
        v = _make_shifted_vector()
        result = apply_time_decay(v, 0.0)
        for name in AxisName:
            assert result.axes[name].valor_actual == pytest.approx(v.axes[name].valor_actual)

    def test_negative_elapsed_unchanged(self):
        v = _make_shifted_vector()
        result = apply_time_decay(v, -100.0)
        for name in AxisName:
            assert result.axes[name].valor_actual == pytest.approx(v.axes[name].valor_actual)

    def test_large_elapsed_converges_to_media(self):
        v = _make_shifted_vector(0.3)
        result = apply_time_decay(v, 1_000_000.0, tau_hours=4.0)
        for name in AxisName:
            assert result.axes[name].valor_actual == pytest.approx(
                result.axes[name].media, abs=0.001,
            )

    def test_tau_hours_63_percent_decay(self):
        v = _make_shifted_vector(0.2)
        tau = 4.0
        elapsed = tau * 3600.0
        result = apply_time_decay(v, elapsed, tau_hours=tau)
        expected_factor = 1.0 - math.exp(-1.0)

        for name in AxisName:
            original = v.axes[name].valor_actual
            media = v.axes[name].media
            expected = original + expected_factor * (media - original)
            assert result.axes[name].valor_actual == pytest.approx(expected)

    def test_half_tau_partial_decay(self):
        v = _make_shifted_vector(0.2)
        tau = 4.0
        elapsed = tau * 3600.0 / 2
        result = apply_time_decay(v, elapsed, tau_hours=tau)

        for name in AxisName:
            original = v.axes[name].valor_actual
            media = v.axes[name].media
            assert result.axes[name].valor_actual != pytest.approx(original)
            assert result.axes[name].valor_actual != pytest.approx(media)
            distance_before = abs(original - media)
            distance_after = abs(result.axes[name].valor_actual - media)
            assert distance_after < distance_before


class TestSaveRestorePosture:
    def test_save_and_restore_roundtrip(self):
        v = _make_shifted_vector(0.1)
        metadata: dict = {}
        save_posture(metadata, v)

        assert POSTURE_METADATA_KEY in metadata

        with patch("durin.posture.persistence.time.time", return_value=metadata[POSTURE_METADATA_KEY]["timestamp"]):
            restored = restore_posture(metadata)

        assert restored is not None
        for name in AxisName:
            assert restored.axes[name].valor_actual == pytest.approx(
                v.axes[name].valor_actual,
            )

    def test_restore_with_elapsed_time_applies_decay(self):
        v = _make_shifted_vector(0.2)
        metadata: dict = {}
        save_posture(metadata, v)

        future_time = metadata[POSTURE_METADATA_KEY]["timestamp"] + 3600.0
        with patch("durin.posture.persistence.time.time", return_value=future_time):
            restored = restore_posture(metadata, tau_hours=4.0)

        assert restored is not None
        for name in AxisName:
            original = v.axes[name].valor_actual
            media = v.axes[name].media
            if abs(original - media) > 0.01:
                distance_before = abs(original - media)
                distance_after = abs(restored.axes[name].valor_actual - media)
                assert distance_after < distance_before

    def test_restore_missing_metadata_returns_none(self):
        result = restore_posture({})
        assert result is None

    def test_metadata_key_does_not_conflict(self):
        metadata: dict = {"goal_state": {"objective": "test"}, "runtime_checkpoint": {}}
        v = PostureVector.default()
        save_posture(metadata, v)
        assert "goal_state" in metadata
        assert "runtime_checkpoint" in metadata
        assert POSTURE_METADATA_KEY in metadata
