import durin.providers.provider_catalog as pc
from durin.config.schema import Config, ModelEntry
from durin.providers.provider_catalog import ModelInfo


def test_default_preset_takes_params_from_provider_models(monkeypatch):
    monkeypatch.setattr(pc, "_load_index", lambda: {})  # isolate from vendored catalog
    cfg = Config()
    cfg.agents.defaults.model = "glm-5.2"
    cfg.agents.defaults.provider = "zai_coding_plan"
    cfg.providers.zai_coding_plan.models = {
        "glm-5.2": ModelEntry(context_window_tokens=1_000_000, max_tokens=131_072)
    }
    p = cfg.resolve_default_preset()
    assert p.context_window_tokens == 1_000_000
    assert p.max_tokens == 131_072


def test_default_preset_falls_back_to_catalog(monkeypatch):
    monkeypatch.setattr(
        pc, "_load_index",
        lambda: {"zai_coding_plan": [ModelInfo(id="glm-5.2", max_input_tokens=1_000_000)]},
    )
    cfg = Config()
    cfg.agents.defaults.model = "glm-5.2"
    cfg.agents.defaults.provider = "zai_coding_plan"
    assert cfg.resolve_default_preset().context_window_tokens == 1_000_000


def test_default_preset_entry_wins_over_catalog(monkeypatch):
    monkeypatch.setattr(
        pc, "_load_index",
        lambda: {"zai_coding_plan": [ModelInfo(id="glm-5.2", max_input_tokens=500_000)]},
    )
    cfg = Config()
    cfg.agents.defaults.model = "glm-5.2"
    cfg.agents.defaults.provider = "zai_coding_plan"
    cfg.providers.zai_coding_plan.models = {
        "glm-5.2": ModelEntry(context_window_tokens=1_000_000)
    }
    assert cfg.resolve_default_preset().context_window_tokens == 1_000_000  # entry > catalog


def test_default_preset_uses_agents_defaults_when_no_override(monkeypatch):
    monkeypatch.setattr(pc, "_load_index", lambda: {})
    cfg = Config()  # provider "auto" → neither entry nor catalog
    p = cfg.resolve_default_preset()
    assert p.context_window_tokens == cfg.agents.defaults.context_window_tokens
    assert p.max_tokens == cfg.agents.defaults.max_tokens
