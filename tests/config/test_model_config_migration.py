from durin.config.loader import _migrate_config
from durin.config.schema import Config


def test_presets_and_defaults_seed_provider_models():
    data = {
        "agents": {"defaults": {
            "model": "glm-5.2", "provider": "zai_coding_plan",
            "contextWindowTokens": 1_000_000,
        }},
        "modelPresets": {"fast": {
            "model": "glm-5.1", "provider": "zhipu", "contextWindowTokens": 200_000,
        }},
    }
    out = _migrate_config(data)
    # The persisted providers dict is keyed by the camelCase field alias.
    assert out["providers"]["zaiCodingPlan"]["models"]["glm-5.2"]["contextWindowTokens"] == 1_000_000
    assert out["providers"]["zhipu"]["models"]["glm-5.1"]["contextWindowTokens"] == 200_000
    # And it validates into the real config model (camelCase merges cleanly).
    cfg = Config.model_validate(out)
    assert cfg.providers.zai_coding_plan.models["glm-5.2"].context_window_tokens == 1_000_000
    assert cfg.providers.zhipu.models["glm-5.1"].context_window_tokens == 200_000


def test_migration_merges_into_existing_provider_block():
    data = {
        "providers": {"zaiCodingPlan": {"apiKey": "sk-x"}},
        "agents": {"defaults": {
            "model": "glm-5.2", "provider": "zai_coding_plan",
            "contextWindowTokens": 1_000_000,
        }},
    }
    out = _migrate_config(data)
    # api key preserved AND models added under the same block.
    assert out["providers"]["zaiCodingPlan"]["apiKey"] == "sk-x"
    assert out["providers"]["zaiCodingPlan"]["models"]["glm-5.2"]["contextWindowTokens"] == 1_000_000
    cfg = Config.model_validate(out)
    assert cfg.providers.zai_coding_plan.api_key == "sk-x"
    assert cfg.providers.zai_coding_plan.models["glm-5.2"].context_window_tokens == 1_000_000


def test_migration_is_idempotent_and_non_clobbering():
    data = {
        "providers": {"zaiCodingPlan": {"models": {"glm-5.2": {"contextWindowTokens": 42}}}},
        "agents": {"defaults": {
            "model": "glm-5.2", "provider": "zai_coding_plan",
            "contextWindowTokens": 1_000_000,
        }},
    }
    out = _migrate_config(_migrate_config(data))
    # the user-set entry is preserved, not overwritten by defaults
    assert out["providers"]["zaiCodingPlan"]["models"]["glm-5.2"]["contextWindowTokens"] == 42


def test_migration_skips_auto_provider():
    data = {"agents": {"defaults": {"model": "x", "provider": "auto", "contextWindowTokens": 5}}}
    out = _migrate_config(data)
    assert "auto" not in out.get("providers", {})


def test_empty_config_still_loads():
    cfg = Config.model_validate(_migrate_config({}))
    assert isinstance(cfg, Config)
