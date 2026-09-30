"""Every model run gets the model's own limits.

A model preset, an ad-hoc ``provider model`` pick, an inline fallback and an
aux model all resolve an unset ``context_window_tokens`` / ``max_tokens``
the way the default preset always did: the user's ``providers.<p>.models``
entry, then the catalog, then ``agents.defaults``. A value set on the preset
itself wins. A configured value above what the model really accepts (the
catalog) is capped to it, with one warning, because the provider rejects a
prompt the window claims should fit.
"""

from __future__ import annotations

import json
import logging

import pytest

import durin.config.schema as schema
import durin.providers.provider_catalog as pc
from durin.config.schema import (
    AgentDefaults,
    Config,
    InlineFallbackConfig,
    ModelEntry,
    ModelPresetConfig,
)
from durin.providers.provider_catalog import ModelInfo

_GLM_53 = ModelInfo(id="glm-5.3", max_input_tokens=1_000_000, max_output_tokens=131_072)
_GLM_TURBO = ModelInfo(id="glm-5-turbo", max_input_tokens=200_000, max_output_tokens=131_072)


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    """A fixed catalog, and a fresh warn-once memory per test."""
    monkeypatch.setattr(pc, "_load_index", lambda: {"zai_coding_plan": [_GLM_53, _GLM_TURBO]})
    monkeypatch.setattr(schema, "_LIMIT_CAP_WARNED", set())


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def warnings_logged():
    """Warnings from the config schema, captured on its own logger so a
    bridge that stops propagation (the gateway's loguru bridge) cannot hide
    them."""
    handler = _Records()
    target = logging.getLogger("durin.config.schema")
    target.addHandler(handler)
    try:
        yield handler.messages
    finally:
        target.removeHandler(handler)


def _config(**presets: ModelPresetConfig) -> Config:
    cfg = Config()
    cfg.agents.defaults.provider = "zai_coding_plan"
    cfg.agents.defaults.model = "glm-5.3"
    cfg.model_presets.update(presets)
    return cfg


# --- "unset" is representable -------------------------------------------------


def test_a_preset_that_names_no_limits_leaves_them_unset() -> None:
    preset = ModelPresetConfig(model="glm-5.3", provider="zai_coding_plan")
    assert preset.max_tokens is None
    assert preset.context_window_tokens is None


def test_an_inline_fallback_that_names_no_limits_leaves_them_unset() -> None:
    fallback = InlineFallbackConfig(model="glm-5.3", provider="zai_coding_plan")
    assert fallback.max_tokens is None
    assert fallback.context_window_tokens is None


def test_unset_preset_limits_stay_unset_through_save_and_load(tmp_path) -> None:
    from durin.config.loader import load_config, save_config

    path = tmp_path / "config.json"
    save_config(_config(judge=ModelPresetConfig(model="glm-5.3", provider="zai_coding_plan")), path)
    written = json.loads(path.read_text())["model_presets"]["judge"]
    assert "max_tokens" not in written
    assert "context_window_tokens" not in written
    reloaded = load_config(path).model_presets["judge"]
    assert reloaded.max_tokens is None
    assert reloaded.context_window_tokens is None


def test_an_explicit_preset_limit_is_saved_even_at_the_old_schema_default(tmp_path) -> None:
    """8,192 / 65,536 used to be the schema defaults, so a save dropped them;
    now an explicit value is the user's and must survive a save."""
    from durin.config.loader import load_config, save_config

    path = tmp_path / "config.json"
    save_config(_config(small=ModelPresetConfig(
        model="glm-5.3", provider="zai_coding_plan", max_tokens=8192, context_window_tokens=65_536,
    )), path)
    written = json.loads(path.read_text())["model_presets"]["small"]
    assert written["max_tokens"] == 8192
    assert written["context_window_tokens"] == 65_536
    resolved = load_config(path).resolve_preset("small")
    assert (resolved.context_window_tokens, resolved.max_tokens) == (65_536, 8192)


# --- a named preset resolves like the default preset ------------------------------


def test_a_named_preset_without_limits_takes_the_catalogs() -> None:
    cfg = _config(judge=ModelPresetConfig(model="glm-5.3", provider="zai_coding_plan", temperature=0.0))
    resolved = cfg.resolve_preset("judge")
    assert resolved.context_window_tokens == 1_000_000
    assert resolved.max_tokens == 131_072
    assert resolved.temperature == 0.0


def test_a_named_preset_takes_the_users_model_entry_before_the_catalog() -> None:
    cfg = _config(judge=ModelPresetConfig(model="glm-5.3", provider="zai_coding_plan"))
    cfg.providers.zai_coding_plan.models["glm-5.3"] = ModelEntry(
        context_window_tokens=231_072, max_tokens=32_000,
    )
    resolved = cfg.resolve_preset("judge")
    assert (resolved.context_window_tokens, resolved.max_tokens) == (231_072, 32_000)


def test_an_uncataloged_named_preset_falls_back_to_agents_defaults() -> None:
    cfg = _config(local=ModelPresetConfig(model="my-local-model", provider="zai_coding_plan"))
    cfg.agents.defaults.context_window_tokens = 32_768
    cfg.agents.defaults.max_tokens = 4096
    resolved = cfg.resolve_preset("local")
    assert (resolved.context_window_tokens, resolved.max_tokens) == (32_768, 4096)


def test_a_limit_set_on_the_preset_wins_over_the_entry_and_the_catalog() -> None:
    cfg = _config(cheap=ModelPresetConfig(
        model="glm-5.3", provider="zai_coding_plan", context_window_tokens=100_000, max_tokens=2048,
    ))
    cfg.providers.zai_coding_plan.models["glm-5.3"] = ModelEntry(context_window_tokens=231_072)
    resolved = cfg.resolve_preset("cheap")
    assert (resolved.context_window_tokens, resolved.max_tokens) == (100_000, 2048)


def test_the_active_named_preset_resolves_too() -> None:
    cfg = _config(judge=ModelPresetConfig(model="glm-5.3", provider="zai_coding_plan"))
    cfg.agents.defaults.model_preset = "judge"
    assert cfg.resolve_preset().context_window_tokens == 1_000_000


def test_resolving_does_not_touch_the_stored_preset() -> None:
    stored = ModelPresetConfig(model="glm-5.3", provider="zai_coding_plan")
    cfg = _config(judge=stored)
    cfg.resolve_preset("judge")
    assert stored.context_window_tokens is None
    assert cfg.model_presets["judge"].max_tokens is None


def test_resolve_preset_limits_fills_a_preset_object() -> None:
    cfg = _config()
    resolved = cfg.resolve_preset_limits(ModelPresetConfig(model="glm-5-turbo", provider="zai_coding_plan"))
    assert (resolved.context_window_tokens, resolved.max_tokens) == (200_000, 131_072)


# --- a configured value above the model's real one is capped -------------------------


def test_an_entry_window_above_the_models_real_one_is_capped(warnings_logged) -> None:
    cfg = _config()
    cfg.agents.defaults.model = "glm-5-turbo"
    cfg.providers.zai_coding_plan.models["glm-5-turbo"] = ModelEntry(context_window_tokens=231_072)
    assert cfg.resolve_default_preset().context_window_tokens == 200_000
    assert len(warnings_logged) == 1
    message = warnings_logged[0]
    for part in ("zai_coding_plan", "glm-5-turbo", "231,072", "200,000"):
        assert part in message


def test_a_preset_limit_above_the_models_real_one_is_capped(warnings_logged) -> None:
    cfg = _config(big=ModelPresetConfig(
        model="glm-5-turbo", provider="zai_coding_plan", context_window_tokens=500_000, max_tokens=200_000,
    ))
    resolved = cfg.resolve_preset("big")
    assert (resolved.context_window_tokens, resolved.max_tokens) == (200_000, 131_072)
    assert len(warnings_logged) == 2
    assert any("max_tokens" in m and "131,072" in m for m in warnings_logged)


def test_the_cap_warns_once_however_often_it_resolves(warnings_logged) -> None:
    cfg = _config()
    cfg.agents.defaults.model = "glm-5-turbo"
    cfg.providers.zai_coding_plan.models["glm-5-turbo"] = ModelEntry(context_window_tokens=231_072)
    for _ in range(5):
        cfg.resolve_default_preset()
    assert len(warnings_logged) == 1


def test_a_value_below_the_models_real_one_is_a_deliberate_cap(warnings_logged) -> None:
    cfg = _config()
    cfg.providers.zai_coding_plan.models["glm-5.3"] = ModelEntry(context_window_tokens=231_072)
    assert cfg.resolve_default_preset().context_window_tokens == 231_072
    assert warnings_logged == []


def test_an_uncataloged_model_is_never_capped(warnings_logged) -> None:
    cfg = _config()
    cfg.agents.defaults.model = "my-local-model"
    cfg.providers.zai_coding_plan.models["my-local-model"] = ModelEntry(context_window_tokens=2_000_000)
    assert cfg.resolve_default_preset().context_window_tokens == 2_000_000
    assert warnings_logged == []


# --- the other preset-like paths -----------------------------------------------------


def test_an_adhoc_pick_of_an_uncataloged_model_falls_back_to_agents_defaults() -> None:
    from durin.command.builtin import adhoc_preset_config

    cfg = _config()
    cfg.agents.defaults.context_window_tokens = 48_000
    cfg.agents.defaults.max_tokens = 6000
    preset = adhoc_preset_config(cfg, "zai_coding_plan", "my-local-model")
    assert (preset.context_window_tokens, preset.max_tokens) == (48_000, 6000)


def test_an_adhoc_pick_is_capped_like_any_configured_value(warnings_logged) -> None:
    from durin.command.builtin import adhoc_preset_config

    cfg = _config()
    cfg.providers.zai_coding_plan.models["glm-5-turbo"] = ModelEntry(context_window_tokens=231_072)
    assert adhoc_preset_config(cfg, "zai_coding_plan", "glm-5-turbo").context_window_tokens == 200_000
    assert len(warnings_logged) == 1


def test_an_adhoc_pick_without_a_config_uses_the_catalog_then_the_schema_defaults() -> None:
    from durin.command.builtin import adhoc_preset_config

    cataloged = adhoc_preset_config(None, "zai_coding_plan", "glm-5.3")
    assert (cataloged.context_window_tokens, cataloged.max_tokens) == (1_000_000, 131_072)
    unknown = adhoc_preset_config(None, "zai_coding_plan", "my-local-model")
    defaults = AgentDefaults()
    assert (unknown.context_window_tokens, unknown.max_tokens) == (
        defaults.context_window_tokens, defaults.max_tokens,
    )


def test_an_inline_fallback_resolves_its_own_models_limits_not_the_primarys() -> None:
    from durin.providers.factory import _resolve_fallback_presets

    cfg = _config()
    cfg.agents.defaults.fallback_models = [
        InlineFallbackConfig(model="glm-5-turbo", provider="zai_coding_plan"),
    ]
    [fallback] = _resolve_fallback_presets(cfg, cfg.resolve_default_preset())
    assert (fallback.context_window_tokens, fallback.max_tokens) == (200_000, 131_072)


def test_a_named_fallback_without_limits_caps_the_window_at_its_real_one() -> None:
    from durin.providers.factory import preset_context_window

    cfg = _config(turbo=ModelPresetConfig(model="glm-5-turbo", provider="zai_coding_plan"))
    cfg.agents.defaults.fallback_models = ["turbo"]
    assert preset_context_window(cfg, cfg.resolve_default_preset()) == 200_000


def test_make_provider_resolves_a_preset_object_that_names_no_limits(monkeypatch) -> None:
    from durin.providers.factory import build_provider_snapshot, make_provider

    cfg = _config(judge=ModelPresetConfig(model="glm-5.3", provider="zai_coding_plan"))
    cfg.providers.zai_coding_plan.api_key = "sk-test"
    provider = make_provider(cfg, preset=cfg.model_presets["judge"])
    assert provider.generation.max_tokens == 131_072
    snapshot = build_provider_snapshot(cfg, preset_name="judge")
    assert snapshot.context_window_tokens == 1_000_000
    assert snapshot.provider.generation.max_tokens == 131_072


def test_an_aux_model_placed_on_the_default_preset_takes_its_own_limits() -> None:
    """The judge / dream model rides a copy of the default preset; its limits
    must be its own model's, not the default model's."""
    from durin.memory.model_resolve import resolve_aux_preset

    cfg = _config()
    cfg.agents.defaults.model = "glm-5-turbo"
    cfg.providers.zai_coding_plan.api_key = "sk-test"
    cfg.skills.security.llm_judge.model = "glm-5.3"
    cfg.skills.security.llm_judge.provider = "zai_coding_plan"
    preset = resolve_aux_preset(cfg, purpose="judge")
    resolved = cfg.resolve_preset_limits(preset)
    assert resolved.model == "glm-5.3"
    assert resolved.context_window_tokens == 1_000_000


# --- the default preset's chain is unchanged -----------------------------------------


def test_the_default_preset_still_reads_entry_then_catalog_then_defaults() -> None:
    cfg = _config()
    assert cfg.resolve_default_preset().context_window_tokens == 1_000_000
    cfg.providers.zai_coding_plan.models["glm-5.3"] = ModelEntry(max_tokens=16_000)
    assert cfg.resolve_default_preset().max_tokens == 16_000
    cfg.agents.defaults.model = "my-local-model"
    assert cfg.resolve_default_preset().context_window_tokens == cfg.agents.defaults.context_window_tokens


def test_a_preset_whose_model_routes_to_no_provider_falls_back_to_agents_defaults() -> None:
    """No provider is configured, so "auto" routes nowhere: there is no entry
    or catalog row to read."""
    cfg = _config(auto=ModelPresetConfig(model="glm-5.3"))
    cfg.agents.defaults.context_window_tokens = 40_000
    assert cfg.resolve_preset("auto").context_window_tokens == 40_000


# --- the provider a run actually uses, not the provider string as written --------------


def _routed_config(**presets: ModelPresetConfig) -> Config:
    """Only zai_coding_plan is configured, and agents.defaults carries the
    onboarding wizard's window for the default model."""
    cfg = _config(**presets)
    cfg.providers.zai_coding_plan.api_key = "sk-test"
    cfg.agents.defaults.context_window_tokens = 1_000_000
    cfg.agents.defaults.max_tokens = 131_072
    return cfg


def test_an_auto_preset_takes_the_limits_of_the_provider_it_runs_on() -> None:
    cfg = _routed_config(turbo=ModelPresetConfig(model="glm-5-turbo"))
    resolved = cfg.resolve_preset("turbo")
    assert cfg.get_provider_name(resolved.model, preset=resolved) == "zai_coding_plan"
    assert (resolved.context_window_tokens, resolved.max_tokens) == (200_000, 131_072)


def test_an_auto_fallback_caps_the_chat_window_at_its_real_one() -> None:
    from durin.providers.factory import build_provider_snapshot

    cfg = _routed_config(turbo=ModelPresetConfig(model="glm-5-turbo"))
    cfg.agents.defaults.fallback_models = ["turbo"]
    assert build_provider_snapshot(cfg).context_window_tokens == 200_000
    assert build_provider_snapshot(cfg, preset_name="turbo").context_window_tokens == 200_000


@pytest.mark.parametrize("spelling", ["zai-coding-plan", "zaiCodingPlan", "zai_coding_plan"])
def test_a_provider_alias_reads_the_same_entry_and_catalog(spelling: str) -> None:
    cfg = _routed_config(turbo=ModelPresetConfig(model="glm-5-turbo", provider=spelling))
    assert cfg.resolve_preset("turbo").context_window_tokens == 200_000
    cfg.providers.zai_coding_plan.models["glm-5-turbo"] = ModelEntry(max_tokens=32_000)
    assert cfg.resolve_preset("turbo").max_tokens == 32_000


def test_an_auto_default_model_takes_the_limits_of_the_provider_it_runs_on() -> None:
    cfg = _config()
    cfg.agents.defaults.provider = "auto"
    cfg.providers.zai_coding_plan.api_key = "sk-test"
    preset = cfg.resolve_default_preset()
    assert (preset.context_window_tokens, preset.max_tokens) == (1_000_000, 131_072)


def test_a_local_models_limits_never_ask_its_server(monkeypatch) -> None:
    """A local provider's live model list carries ids only, never limits,
    and asking an unreachable server blocks for seconds; the limits come
    from the static catalog, which a reachable server does not replace."""
    import durin.providers.local_models as local_models
    from durin.providers.factory import build_provider_snapshot

    calls: list[str] = []

    def _live(api_base, api_key=None, timeout=3.0):
        calls.append(api_base)
        return ["qwen-local"]  # a reachable server that lists the model

    monkeypatch.setattr(local_models, "list_local_models", _live)
    monkeypatch.setattr(pc, "_load_index", lambda: {
        "zai_coding_plan": [_GLM_53],
        "lm_studio": [ModelInfo(id="qwen-local", max_input_tokens=32_768, max_output_tokens=8_192)],
    })
    cfg = _routed_config()
    cfg.providers.lm_studio.api_base = "http://127.0.0.1:9/v1"
    cfg.providers.vllm.api_base = "http://10.255.255.1:8000/v1"
    cfg.agents.defaults.fallback_models = [
        InlineFallbackConfig(model="qwen-local", provider="lm_studio"),
        InlineFallbackConfig(model="qwen3-coder-30b", provider="vllm"),
    ]
    resolved = cfg.resolve_preset_limits(ModelPresetConfig(model="qwen-local", provider="lm_studio"))
    assert (resolved.context_window_tokens, resolved.max_tokens) == (32_768, 8_192)
    build_provider_snapshot(cfg)
    assert calls == []


def _with_local_fallback(**fields) -> Config:
    """glm-5.3 (a 1M window in the catalog) with one inline fallback on a
    local model the catalog does not know; agents.defaults left alone."""
    cfg = _config()
    cfg.providers.zai_coding_plan.api_key = "sk-test"
    cfg.providers.ollama.api_base = "http://127.0.0.1:9/v1"
    cfg.agents.defaults.fallback_models = [
        InlineFallbackConfig(model="qwen3-coder:30b", provider="ollama", **fields),
    ]
    return cfg


def test_an_unknown_fallback_window_does_not_shrink_a_known_one() -> None:
    """The fallback's window would only be agents.defaults' guess (65,536):
    it must not cut the chat's known 1M window to that."""
    from durin.providers.factory import build_provider_snapshot, preset_window_cap

    cfg = _with_local_fallback()
    assert build_provider_snapshot(cfg).context_window_tokens == 1_000_000
    assert preset_window_cap(cfg, cfg.resolve_default_preset()) == (1_000_000, None)


def test_a_fallback_whose_window_is_declared_still_caps_the_chat_window() -> None:
    from durin.providers.factory import build_provider_snapshot, preset_window_cap

    cfg = _with_local_fallback(context_window_tokens=32_768)
    assert build_provider_snapshot(cfg).context_window_tokens == 32_768
    window, capping = preset_window_cap(cfg, cfg.resolve_default_preset())
    assert window == 32_768
    assert capping is not None and capping.label == "agents.defaults.fallback_models.0"
    assert (capping.provider, capping.model) == ("ollama", "qwen3-coder:30b")
    cfg = _with_local_fallback()
    cfg.providers.ollama.models["qwen3-coder:30b"] = ModelEntry(context_window_tokens=16_384)
    assert build_provider_snapshot(cfg).context_window_tokens == 16_384


def test_a_named_fallback_that_caps_the_window_is_named() -> None:
    from durin.providers.factory import preset_window_cap

    cfg = _routed_config(turbo=ModelPresetConfig(model="glm-5-turbo", provider="zai_coding_plan"))
    cfg.agents.defaults.fallback_models = ["turbo"]
    window, capping = preset_window_cap(cfg, cfg.resolve_default_preset())
    assert window == 200_000
    assert capping is not None and capping.label == "turbo"


@pytest.mark.parametrize("key", ["zai_coding_plan/glm-5-turbo", "glm-5-turbo"])
def test_a_capability_override_is_the_models_real_limit(key: str, warnings_logged) -> None:
    """When the catalog is stale or wrong, model_capabilities is the user's
    word on the model: a configured value up to it is not capped, and an
    unset one takes it."""
    from durin.config.schema import ModelCapabilityOverride

    cfg = _config(turbo=ModelPresetConfig(model="glm-5-turbo", provider="zai_coding_plan"))
    cfg.model_capabilities[key] = ModelCapabilityOverride(max_input_tokens=300_000, max_output_tokens=150_000)
    assert cfg.resolve_preset("turbo").context_window_tokens == 300_000
    assert cfg.resolve_preset("turbo").max_tokens == 150_000
    cfg.providers.zai_coding_plan.models["glm-5-turbo"] = ModelEntry(context_window_tokens=280_000)
    assert cfg.resolve_preset("turbo").context_window_tokens == 280_000
    cfg.providers.zai_coding_plan.models["glm-5-turbo"] = ModelEntry(context_window_tokens=400_000)
    assert cfg.resolve_preset("turbo").context_window_tokens == 300_000
    assert any("300,000" in m and "400,000" in m for m in warnings_logged)


def test_a_provider_qualified_override_wins_over_the_bare_one() -> None:
    from durin.config.schema import ModelCapabilityOverride

    cfg = _config(turbo=ModelPresetConfig(model="glm-5-turbo", provider="zai_coding_plan"))
    cfg.model_capabilities["glm-5-turbo"] = ModelCapabilityOverride(max_input_tokens=300_000)
    cfg.model_capabilities["zai_coding_plan/glm-5-turbo"] = ModelCapabilityOverride(max_input_tokens=250_000)
    assert cfg.resolve_preset("turbo").context_window_tokens == 250_000


def test_routed_provider_names_the_registry_key_a_run_goes_to() -> None:
    cfg = _routed_config()
    assert cfg.routed_provider("auto", "glm-5-turbo") == "zai_coding_plan"
    assert cfg.routed_provider("zai-coding-plan", "glm-5-turbo") == "zai_coding_plan"
    assert cfg.routed_provider("zaiCodingPlan", "glm-5-turbo") == "zai_coding_plan"
    assert _config().routed_provider("auto", "glm-5-turbo") == "auto"
