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
    # New blocks are created under the canonical snake_case provider name, with
    # snake_case entry params (camelCase input is still accepted on the way in).
    assert out["providers"]["zai_coding_plan"]["models"]["glm-5.2"]["context_window_tokens"] == 1_000_000
    assert out["providers"]["zhipu"]["models"]["glm-5.1"]["context_window_tokens"] == 200_000
    # And it validates into the real config model.
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
    # Merged into the EXISTING (legacy camel) block; no snake duplicate created
    # that would shadow it on validation.
    assert "zai_coding_plan" not in out["providers"]
    assert out["providers"]["zaiCodingPlan"]["apiKey"] == "sk-x"
    assert out["providers"]["zaiCodingPlan"]["models"]["glm-5.2"]["context_window_tokens"] == 1_000_000
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


def test_snake_keyed_default_provider_keeps_api_key():
    """Regression: a snake_case-keyed provider block (the canonical on-disk form
    after the casing migration) that is ALSO the default provider must keep its
    api_key. The seeder used to camelCase the provider value to find the block;
    against a snake-keyed dict that missed, created an empty ``zaiCodingPlan``
    duplicate, and the empty alias block shadowed the real one on validation —
    silently dropping the default provider's api_key."""
    data = {
        "providers": {
            "zai_coding_plan": {
                "api_key": "${secret:ZHIPU_API_KEY}",
                "models": {"glm-5.2": {"reasoning_effort": "high"}},
            }
        },
        # Inline params on defaults make the seeder's ``entry`` non-empty, so it
        # proceeds to find/create the provider block (the buggy path). This
        # mirrors a real default config (reasoning_effort, context window, …).
        "agents": {"defaults": {
            "model": "glm-5.2", "provider": "zai_coding_plan",
            "reasoning_effort": "high", "context_window_tokens": 65536,
        }},
    }
    out = _migrate_config(data)
    # No duplicate camelCase block was created.
    assert "zaiCodingPlan" not in out["providers"]
    assert out["providers"]["zai_coding_plan"]["api_key"] == "${secret:ZHIPU_API_KEY}"
    cfg = Config.model_validate(out)
    assert cfg.providers.zai_coding_plan.api_key == "${secret:ZHIPU_API_KEY}"
    assert "glm-5.2" in cfg.providers.zai_coding_plan.models


def test_empty_config_still_loads():
    cfg = Config.model_validate(_migrate_config({}))
    assert isinstance(cfg, Config)
