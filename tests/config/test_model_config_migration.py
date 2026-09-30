"""Loading a config never writes a model's entry on its own.

A ``providers.<p>.models[<model>]`` entry is the model's own configuration:
every run on that model — the chat, a ``/model`` pick, a workflow node that
names it — reads it before the catalog. A named preset and
``agents.defaults`` are not entries: a preset's values belong to runs on
that preset, and ``agents.defaults`` is where the resolution falls back for
a model with no entry and no catalog record. Copying either into an entry
on load pinned their values onto the model: a preset's temperature 0.0
became every default-model run's temperature, and ``agents.defaults``'
window stuck to whichever model was the default, and then to the next one.
"""

import copy
import json

import pytest

import durin.providers.provider_catalog as pc
from durin.config.loader import _migrate_config, load_config, save_config
from durin.config.schema import Config
from durin.providers.provider_catalog import ModelInfo


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    monkeypatch.setattr(pc, "_load_index", lambda: {"zai_coding_plan": [
        ModelInfo(id="glm-5.3", max_input_tokens=1_000_000, max_output_tokens=131_072),
    ]})


def _write(tmp_path, data: dict):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_presets_temperature_stays_on_the_preset(tmp_path):
    """A preset `precise` at temperature 0.0 on glm-5.3, while glm-5.3 has no
    entry: the default model (also glm-5.3, at the agents.defaults
    temperature) must not start running at 0.0."""
    path = _write(tmp_path, {
        "agents": {"defaults": {"model": "glm-5.3", "provider": "zai_coding_plan"}},
        "modelPresets": {"precise": {"model": "glm-5.3", "provider": "zai_coding_plan", "temperature": 0.0}},
    })
    cfg = load_config(path)
    default_temperature = cfg.agents.defaults.temperature
    assert default_temperature != 0.0
    assert cfg.resolve_default_preset().temperature == default_temperature
    assert cfg.resolve_preset("precise").temperature == 0.0
    assert cfg.providers.zai_coding_plan.models == {}
    # A save persists whatever the load produced; still no entry.
    save_config(cfg, path)
    reloaded = load_config(path)
    assert reloaded.providers.zai_coding_plan.models == {}
    assert reloaded.resolve_default_preset().temperature == default_temperature


def test_agents_defaults_window_is_not_pinned_onto_the_default_model(tmp_path):
    """A window left in agents.defaults (set for an earlier model) must not
    become the default model's entry: glm-5.3 runs with its own limits."""
    path = _write(tmp_path, {"agents": {"defaults": {
        "model": "glm-5.3", "provider": "zai_coding_plan",
        "contextWindowTokens": 231_072, "maxTokens": 8192,
    }}})
    cfg = load_config(path)
    preset = cfg.resolve_default_preset()
    assert (preset.context_window_tokens, preset.max_tokens) == (1_000_000, 131_072)
    assert cfg.providers.zai_coding_plan.models == {}


def test_an_edit_of_agents_defaults_takes_effect_after_a_save(tmp_path):
    """A copy of agents.defaults in the model's entry would win over a later
    edit of agents.defaults for that model."""
    path = _write(tmp_path, {"agents": {"defaults": {
        "model": "glm-5.3", "provider": "zai_coding_plan", "temperature": 0.4, "reasoningEffort": "high",
    }}})
    save_config(load_config(path), path)
    cfg = load_config(path)
    cfg.agents.defaults.temperature = 0.2
    cfg.agents.defaults.reasoning_effort = "low"
    save_config(cfg, path)
    preset = load_config(path).resolve_default_preset()
    assert (preset.temperature, preset.reasoning_effort) == (0.2, "low")


def test_agents_defaults_still_serve_a_model_the_catalog_does_not_know(tmp_path):
    path = _write(tmp_path, {"agents": {"defaults": {
        "model": "my-local-model", "provider": "zai_coding_plan",
        "contextWindowTokens": 32_768, "maxTokens": 4096, "temperature": 0.3,
    }}})
    preset = load_config(path).resolve_default_preset()
    assert (preset.context_window_tokens, preset.max_tokens, preset.temperature) == (32_768, 4096, 0.3)


def test_an_existing_entry_is_kept_as_it_is(tmp_path):
    path = _write(tmp_path, {
        "providers": {"zai_coding_plan": {"models": {"glm-5.3": {"context_window_tokens": 500_000}}}},
        "agents": {"defaults": {"model": "glm-5.3", "provider": "zai_coding_plan", "contextWindowTokens": 231_072}},
    })
    cfg = load_config(path)
    assert cfg.providers.zai_coding_plan.models["glm-5.3"].context_window_tokens == 500_000
    assert cfg.resolve_default_preset().context_window_tokens == 500_000


def test_the_migration_leaves_providers_presets_and_defaults_untouched():
    data = {
        "providers": {"zai_coding_plan": {
            "api_key": "${secret:ZHIPU_API_KEY}",
            "models": {"glm-5.2": {"reasoning_effort": "high"}},
        }},
        "agents": {"defaults": {
            "model": "glm-5.3", "provider": "zai_coding_plan",
            "reasoning_effort": "high", "context_window_tokens": 65536,
        }},
        "modelPresets": {"fast": {"model": "glm-5.1", "provider": "zhipu", "contextWindowTokens": 200_000}},
    }
    out = _migrate_config(copy.deepcopy(data))
    assert out["providers"] == data["providers"]
    assert out["agents"] == data["agents"]
    assert out["modelPresets"] == data["modelPresets"]
    cfg = Config.model_validate(out)
    assert cfg.providers.zai_coding_plan.api_key == "${secret:ZHIPU_API_KEY}"
    assert cfg.model_presets["fast"].context_window_tokens == 200_000


def test_empty_config_still_loads():
    cfg = Config.model_validate(_migrate_config({}))
    assert isinstance(cfg, Config)
