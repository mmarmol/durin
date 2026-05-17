"""Tests for posture configuration integration."""

from __future__ import annotations

import json

import pytest

from durin.config.schema import AgentDefaults, AxisConfig, PostureConfig
from durin.posture.vector import AxisName


class TestPostureConfig:
    def test_default_disabled(self):
        cfg = PostureConfig()
        assert cfg.enabled is False

    def test_default_axes_match_vector_defaults(self):
        from durin.posture.vector import PostureVector

        cfg = PostureConfig()
        defaults = PostureVector.default()
        for name in AxisName:
            axis_cfg = cfg.axes[name.value]
            axis_state = defaults.axes[name]
            assert axis_cfg.mean == axis_state.mean
            assert axis_cfg.variance == axis_state.variance
            assert axis_cfg.return_force == axis_state.return_force

    def test_custom_axes_override(self):
        cfg = PostureConfig(
            enabled=True,
            axes={
                "caution": AxisConfig(mean=0.7, variance=0.2, return_force=0.4),
                "exploration": AxisConfig(mean=0.3, variance=0.1, return_force=0.5),
                "depth": AxisConfig(mean=0.5, variance=0.15, return_force=0.3),
                "discipline": AxisConfig(mean=0.5, variance=0.15, return_force=0.3),
                "conformity": AxisConfig(mean=0.6, variance=0.15, return_force=0.3),
            },
        )
        assert cfg.enabled is True
        assert cfg.axes["caution"].mean == 0.7

    def test_validation_rejects_media_out_of_range(self):
        with pytest.raises(Exception):
            AxisConfig(mean=1.5, variance=0.15, return_force=0.3)

    def test_validation_rejects_variance_zero(self):
        with pytest.raises(Exception):
            AxisConfig(mean=0.5, variance=0.0, return_force=0.3)

    def test_validation_rejects_variance_too_large(self):
        with pytest.raises(Exception):
            AxisConfig(mean=0.5, variance=0.6, return_force=0.3)

    def test_validation_rejects_return_force_negative(self):
        with pytest.raises(Exception):
            AxisConfig(mean=0.5, variance=0.15, return_force=-0.1)


class TestAgentDefaultsPosture:
    def test_defaults_include_posture(self):
        defaults = AgentDefaults()
        assert hasattr(defaults, "posture")
        assert isinstance(defaults.posture, PostureConfig)
        assert defaults.posture.enabled is False

    def test_backward_compat_without_posture_key(self):
        data = {"model": "anthropic/claude-opus-4-5", "maxTokens": 4096}
        defaults = AgentDefaults.model_validate(data)
        assert defaults.posture.enabled is False

    def test_json_with_posture_parses(self):
        data = {
            "model": "anthropic/claude-opus-4-5",
            "posture": {
                "enabled": True,
                "axes": {
                    "caution": {"mean": 0.7, "variance": 0.2, "return_force": 0.4},
                    "exploration": {"mean": 0.3, "variance": 0.1, "return_force": 0.5},
                    "depth": {"mean": 0.5, "variance": 0.15, "return_force": 0.3},
                    "discipline": {"mean": 0.5, "variance": 0.15, "return_force": 0.3},
                    "conformity": {"mean": 0.6, "variance": 0.15, "return_force": 0.3},
                },
            },
        }
        defaults = AgentDefaults.model_validate(data)
        assert defaults.posture.enabled is True
        assert defaults.posture.axes["caution"].mean == 0.7

    def test_camel_case_serialization(self):
        cfg = PostureConfig()
        dumped = cfg.model_dump(by_alias=True)
        assert "enabled" in dumped
        assert "axes" in dumped

    def test_model_dump_roundtrip(self):
        defaults = AgentDefaults()
        dumped = json.loads(defaults.model_dump_json(by_alias=True))
        restored = AgentDefaults.model_validate(dumped)
        assert restored.posture.enabled == defaults.posture.enabled
        assert len(restored.posture.axes) == 5
