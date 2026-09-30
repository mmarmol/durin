"""`durin doctor` lists every configured model limit that differs from the
catalog: a window or output cap above the model's real one warns (the run
caps it, but the config says something untrue and a stale value there spread
to other models), one below is listed as information (a deliberate cost cap
is legitimate, but a forgotten one shrinks every run on that model)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import durin.providers.provider_catalog as pc
from durin.cli.doctor import check_model_limits
from durin.config.schema import Config, InlineFallbackConfig, ModelEntry, ModelPresetConfig
from durin.providers.provider_catalog import ModelInfo


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    monkeypatch.setattr(pc, "_load_index", lambda: {"zai_coding_plan": [
        ModelInfo(id="glm-5.3", max_input_tokens=1_000_000, max_output_tokens=131_072),
        ModelInfo(id="glm-5-turbo", max_input_tokens=200_000, max_output_tokens=131_072),
    ]})


def _cfg() -> Config:
    c = Config()
    c.agents.defaults.provider = "zai_coding_plan"
    c.agents.defaults.model = "glm-5.3"
    c.providers.zai_coding_plan.api_key = "k"
    return c


def _run(cfg: Config):
    with patch("durin.cli.doctor.load_config", return_value=cfg):
        return check_model_limits()


def test_ok_when_no_limit_is_configured() -> None:
    r = _run(_cfg())
    assert r.status == "ok"
    assert r.category == "models"


def test_a_window_above_the_catalog_warns_with_both_values() -> None:
    cfg = _cfg()
    cfg.providers.zai_coding_plan.models["glm-5-turbo"] = ModelEntry(context_window_tokens=231_072)
    r = _run(cfg)
    assert r.status == "warn"
    assert "zai_coding_plan" in r.message and "glm-5-turbo" in r.message
    assert "231,072" in r.message and "200,000" in r.message
    assert r.fix


def test_a_window_below_the_catalog_is_listed_but_does_not_warn() -> None:
    cfg = _cfg()
    cfg.providers.zai_coding_plan.models["glm-5.3"] = ModelEntry(context_window_tokens=231_072)
    r = _run(cfg)
    assert r.status == "ok"
    assert "glm-5.3" in r.message
    assert "231,072" in r.message and "1,000,000" in r.message


def test_an_output_cap_above_the_catalog_warns() -> None:
    cfg = _cfg()
    cfg.providers.zai_coding_plan.models["glm-5.3"] = ModelEntry(max_tokens=200_000)
    r = _run(cfg)
    assert r.status == "warn"
    assert "200,000" in r.message and "131,072" in r.message


def test_the_box_config_lists_every_model_that_differs() -> None:
    cfg = _cfg()
    models = cfg.providers.zai_coding_plan.models
    models["glm-5.3"] = ModelEntry(context_window_tokens=231_072)
    models["glm-5-turbo"] = ModelEntry(context_window_tokens=231_072)
    r = _run(cfg)
    assert r.status == "warn"
    assert "glm-5.3" in r.message and "glm-5-turbo" in r.message


def test_a_named_preset_with_its_own_limits_is_checked() -> None:
    cfg = _cfg()
    cfg.model_presets["big"] = ModelPresetConfig(
        model="glm-5-turbo", provider="zai_coding_plan", context_window_tokens=500_000,
    )
    r = _run(cfg)
    assert r.status == "warn"
    assert "model_presets" in r.message and "big" in r.message


@pytest.mark.parametrize("provider", ["auto", "zai-coding-plan"])
def test_a_preset_is_checked_against_the_provider_it_runs_on(provider: str) -> None:
    """A preset on "auto" (the schema default) or an alias spelling runs on
    zai_coding_plan here, so its window is checked against that catalog."""
    cfg = _cfg()
    cfg.model_presets["turbo"] = ModelPresetConfig(
        model="glm-5-turbo", provider=provider, context_window_tokens=231_072,
    )
    r = _run(cfg)
    assert r.status == "warn"
    assert 'model_presets["turbo"]' in r.message
    assert "231,072" in r.message and "200,000" in r.message


def test_a_capability_override_is_the_real_limit_the_doctor_compares_with() -> None:
    from durin.config.schema import ModelCapabilityOverride

    cfg = _cfg()
    cfg.model_capabilities["zai_coding_plan/glm-5-turbo"] = ModelCapabilityOverride(max_input_tokens=300_000)
    cfg.providers.zai_coding_plan.models["glm-5-turbo"] = ModelEntry(context_window_tokens=300_000)
    assert "glm-5-turbo" not in _run(cfg).message
    cfg.providers.zai_coding_plan.models["glm-5-turbo"] = ModelEntry(context_window_tokens=350_000)
    r = _run(cfg)
    assert r.status == "warn"
    assert "350,000" in r.message and "300,000" in r.message


def test_an_agents_defaults_cap_the_default_model_does_not_use_warns() -> None:
    """A cost cap written to agents.defaults for a catalog model: the model
    runs with its catalog limits, so the cap silently stopped applying."""
    cfg = _cfg()
    cfg.agents.defaults.context_window_tokens = 128_000
    cfg.agents.defaults.max_tokens = 16_000
    r = _run(cfg)
    assert r.status == "warn"
    assert "agents.defaults.context_window_tokens" in r.message
    assert "128,000" in r.message and "1,000,000" in r.message
    assert "agents.defaults.max_tokens" in r.message
    assert "16,000" in r.message and "131,072" in r.message
    assert r.fix and 'providers.zai_coding_plan.models["glm-5.3"].context_window_tokens' in r.fix


@pytest.mark.parametrize("window", [1_000_000, 65_536])
def test_an_agents_defaults_value_the_default_model_matches_or_the_schema_default_is_silent(window: int) -> None:
    cfg = _cfg()
    cfg.agents.defaults.context_window_tokens = window
    r = _run(cfg)
    assert r.status == "ok"
    assert "agents.defaults" not in r.message


def test_an_agents_defaults_cap_on_an_uncataloged_default_model_is_silent() -> None:
    cfg = _cfg()
    cfg.agents.defaults.model = "my-local-model"
    cfg.agents.defaults.context_window_tokens = 32_768
    assert "agents.defaults" not in _run(cfg).message


def test_the_fallback_that_caps_the_chat_window_is_named() -> None:
    cfg = _cfg()
    cfg.model_presets["turbo"] = ModelPresetConfig(model="glm-5-turbo", provider="zai_coding_plan")
    cfg.agents.defaults.fallback_models = ["turbo"]
    r = _run(cfg)
    assert r.status == "ok"
    assert "chat window 200,000" in r.message
    assert "capped by fallback turbo (zai_coding_plan/glm-5-turbo)" in r.message
    assert "1,000,000" in r.message


def test_a_fallback_with_no_known_window_is_listed() -> None:
    """It does not lower the chat window, so a prompt bigger than its real
    window would fail over into an error; declaring its window fixes that."""
    cfg = _cfg()
    cfg.providers.ollama.api_base = "http://127.0.0.1:9/v1"
    cfg.agents.defaults.fallback_models = [
        InlineFallbackConfig(model="qwen3-coder:30b", provider="ollama"),
    ]
    r = _run(cfg)
    assert r.status == "ok"
    assert "agents.defaults.fallback_models.0" in r.message
    assert "ollama/qwen3-coder:30b" in r.message
    assert "no known window" in r.message


def test_an_inline_fallback_with_its_own_limits_is_checked() -> None:
    cfg = _cfg()
    cfg.agents.defaults.fallback_models = [
        InlineFallbackConfig(model="glm-5-turbo", provider="zai_coding_plan", max_tokens=150_000),
    ]
    r = _run(cfg)
    assert r.status == "warn"
    assert "fallback_models" in r.message


def test_values_equal_to_the_catalog_and_uncataloged_models_are_silent() -> None:
    cfg = _cfg()
    cfg.providers.zai_coding_plan.models["glm-5.3"] = ModelEntry(context_window_tokens=1_000_000)
    cfg.providers.zai_coding_plan.models["my-local-model"] = ModelEntry(context_window_tokens=2_000_000)
    r = _run(cfg)
    assert r.status == "ok"
    assert "glm-5.3" not in r.message
    assert "my-local-model" not in r.message


def test_an_unloadable_config_warns() -> None:
    with patch("durin.cli.doctor.load_config", side_effect=ValueError("broken")):
        r = check_model_limits()
    assert r.status == "warn"
