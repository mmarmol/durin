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
            assert axis_cfg.media == axis_state.media
            assert axis_cfg.varianza == axis_state.varianza
            assert axis_cfg.fuerza_retorno == axis_state.fuerza_retorno

    def test_custom_axes_override(self):
        cfg = PostureConfig(
            enabled=True,
            axes={
                "cautela": AxisConfig(media=0.7, varianza=0.2, fuerza_retorno=0.4),
                "exploracion": AxisConfig(media=0.3, varianza=0.1, fuerza_retorno=0.5),
                "profundidad": AxisConfig(media=0.5, varianza=0.15, fuerza_retorno=0.3),
                "disciplina": AxisConfig(media=0.5, varianza=0.15, fuerza_retorno=0.3),
                "conformidad": AxisConfig(media=0.6, varianza=0.15, fuerza_retorno=0.3),
            },
        )
        assert cfg.enabled is True
        assert cfg.axes["cautela"].media == 0.7

    def test_validation_rejects_media_out_of_range(self):
        with pytest.raises(Exception):
            AxisConfig(media=1.5, varianza=0.15, fuerza_retorno=0.3)

    def test_validation_rejects_varianza_zero(self):
        with pytest.raises(Exception):
            AxisConfig(media=0.5, varianza=0.0, fuerza_retorno=0.3)

    def test_validation_rejects_varianza_too_large(self):
        with pytest.raises(Exception):
            AxisConfig(media=0.5, varianza=0.6, fuerza_retorno=0.3)

    def test_validation_rejects_fuerza_retorno_negative(self):
        with pytest.raises(Exception):
            AxisConfig(media=0.5, varianza=0.15, fuerza_retorno=-0.1)


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
                    "cautela": {"media": 0.7, "varianza": 0.2, "fuerza_retorno": 0.4},
                    "exploracion": {"media": 0.3, "varianza": 0.1, "fuerza_retorno": 0.5},
                    "profundidad": {"media": 0.5, "varianza": 0.15, "fuerza_retorno": 0.3},
                    "disciplina": {"media": 0.5, "varianza": 0.15, "fuerza_retorno": 0.3},
                    "conformidad": {"media": 0.6, "varianza": 0.15, "fuerza_retorno": 0.3},
                },
            },
        }
        defaults = AgentDefaults.model_validate(data)
        assert defaults.posture.enabled is True
        assert defaults.posture.axes["cautela"].media == 0.7

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
