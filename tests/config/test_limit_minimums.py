"""A window or output cap is at least 1 token wherever it is set.

A settings editor that saved a cleared number as ``0`` gave a preset
``max_tokens: 0`` — every request on it asked for no output — and a window
of 0 leaves no input budget at all. The schema now refuses a value below 1,
while a config that already holds one still loads: that value reads as unset
(the model's own limit) instead of failing validation, which would put the
whole config back to defaults.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pydantic
import pytest
from typer.testing import CliRunner

from durin.cli.commands import app
from durin.config.loader import load_config
from durin.config.schema import (
    AgentDefaults,
    InlineFallbackConfig,
    ModelEntry,
    ModelPresetConfig,
)

_FIELDS = ("max_tokens", "context_window_tokens")


@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize("model, base", [
    (ModelPresetConfig, {"model": "glm-5.3"}),
    (ModelEntry, {}),
    (InlineFallbackConfig, {"model": "glm-5.3", "provider": "zai_coding_plan"}),
    (AgentDefaults, {}),
])
@pytest.mark.parametrize("value", [0, -1])
def test_a_limit_below_one_is_refused(model, base: dict, field: str, value: int) -> None:
    with pytest.raises(pydantic.ValidationError):
        model.model_validate({**base, field: value})


def _zeros() -> dict:
    """A config as an old settings editor could leave it: a 0 in each place
    a limit is set, next to settings that must survive the load."""
    return {
        "agents": {"defaults": {
            "model": "glm-5.3", "provider": "zai_coding_plan", "temperature": 0.3,
            "maxTokens": 0, "context_window_tokens": 0,
            "fallback_models": [{"model": "glm-5-turbo", "provider": "zai_coding_plan", "max_tokens": 0}],
        }},
        "model_presets": {"fast": {"model": "glm-5-turbo", "provider": "zai_coding_plan", "contextWindowTokens": 0}},
        "providers": {"zai_coding_plan": {"models": {"glm-5.3": {"max_tokens": 0, "temperature": 0.2}}}},
    }


def test_a_config_holding_a_zero_limit_still_loads_with_it_unset(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_zeros()), encoding="utf-8")
    cfg = load_config(path)
    d = cfg.agents.defaults
    assert (d.model, d.temperature) == ("glm-5.3", 0.3)
    assert d.max_tokens == AgentDefaults.model_fields["max_tokens"].default
    assert d.context_window_tokens == AgentDefaults.model_fields["context_window_tokens"].default
    assert d.fallback_models[0].max_tokens is None
    assert cfg.model_presets["fast"].context_window_tokens is None
    entry = cfg.providers.zai_coding_plan.models["glm-5.3"]
    assert (entry.max_tokens, entry.temperature) == (None, 0.2)


def test_config_set_on_a_config_holding_a_zero_limit_works_and_clears_it(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_zeros()), encoding="utf-8")
    with patch("durin.cli.config_cmd.get_config_path", return_value=path), \
         patch("durin.config.loader.get_config_path", return_value=path):
        result = CliRunner().invoke(app, ["config", "set", "agents.defaults.temperature", "0.35"])
    assert result.exit_code == 0, result.output
    saved = json.loads(path.read_text())
    assert saved["agents"]["defaults"]["temperature"] == 0.35
    text = json.dumps(saved)
    assert "max_tokens" not in text and "context_window_tokens" not in text


def test_config_set_refuses_a_zero_limit(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model_presets": {"fast": {"model": "glm-5-turbo"}}}), encoding="utf-8")
    before = path.read_text()
    with patch("durin.cli.config_cmd.get_config_path", return_value=path), \
         patch("durin.config.loader.get_config_path", return_value=path):
        result = CliRunner().invoke(app, ["config", "set", "model_presets.fast.max_tokens", "0"])
    assert result.exit_code == 1
    assert "greater than or equal to 1" in result.output
    assert path.read_text() == before
