"""Tests for deliberation configuration."""

from __future__ import annotations

import pytest

from durin.config.schema import (
    AgentDefaults,
    DeliberationConfig,
    EvaluatorConfig,
    GeneratorRoleConfig,
)


class TestDeliberationConfig:
    def test_default_disabled(self):
        cfg = DeliberationConfig()
        assert cfg.enabled is False

    def test_default_provider(self):
        cfg = DeliberationConfig()
        assert cfg.provider == "ollama"

    def test_default_max_rounds(self):
        cfg = DeliberationConfig()
        assert cfg.max_rounds == 3

    def test_default_generators(self):
        cfg = DeliberationConfig()
        assert "pragmatico" in cfg.generators
        assert "explorador" in cfg.generators
        assert "critico" in cfg.generators
        assert cfg.generators["pragmatico"].temperature == 0.3
        assert cfg.generators["explorador"].temperature == 0.8

    def test_default_evaluators(self):
        cfg = DeliberationConfig()
        assert "avance" in cfg.evaluators
        assert "reversibilidad" in cfg.evaluators

    def test_custom_model_override(self):
        cfg = DeliberationConfig(
            enabled=True,
            generators={
                "pragmatico": GeneratorRoleConfig(model="mistral:7b", temperature=0.5),
                "explorador": GeneratorRoleConfig(model="mistral:7b"),
                "critico": GeneratorRoleConfig(model="mistral:7b"),
            },
        )
        assert cfg.generators["pragmatico"].model == "mistral:7b"

    def test_max_rounds_validation(self):
        with pytest.raises(Exception):
            DeliberationConfig(max_rounds=0)
        with pytest.raises(Exception):
            DeliberationConfig(max_rounds=6)


class TestAgentDefaultsDeliberation:
    def test_defaults_include_deliberation(self):
        defaults = AgentDefaults()
        assert hasattr(defaults, "deliberation")
        assert defaults.deliberation.enabled is False

    def test_backward_compat_without_deliberation_key(self):
        data = {"model": "anthropic/claude-opus-4-5", "maxTokens": 4096}
        defaults = AgentDefaults.model_validate(data)
        assert defaults.deliberation.enabled is False

    def test_json_with_deliberation_parses(self):
        data = {
            "model": "anthropic/claude-opus-4-5",
            "deliberation": {
                "enabled": True,
                "provider": "ollama",
                "maxRounds": 2,
            },
        }
        defaults = AgentDefaults.model_validate(data)
        assert defaults.deliberation.enabled is True
        assert defaults.deliberation.max_rounds == 2
