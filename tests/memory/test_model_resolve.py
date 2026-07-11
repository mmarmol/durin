"""Unit tests for ``durin.memory.model_resolve``.

The contract: every purpose resolves to a full (provider, model) preset. A
bare model name is placed by name-autodetection among CONFIGURED providers —
never blindly paired with the default provider — and a name nothing serves
falls back to the whole default preset (specific-or-default)."""

from __future__ import annotations

from durin.config.schema import AuxModelConfig, Config
from durin.memory.model_resolve import resolve_aux_preset


def _real_cfg(model="glm-5.2", provider="zhipu") -> Config:
    c = Config()
    c.agents.defaults.provider = provider
    c.agents.defaults.model = model
    c.providers.zhipu.api_key = "k-zhipu"
    return c


def test_memory_falls_back_to_default_preset_when_unset() -> None:
    p = resolve_aux_preset(_real_cfg(), purpose="memory")
    assert p.model == "glm-5.2"
    assert p.provider == "zhipu"


def test_judge_falls_back_to_default_preset_when_unset() -> None:
    assert resolve_aux_preset(_real_cfg(), purpose="judge").model == "glm-5.2"


def test_memory_aux_pair_is_honored_verbatim() -> None:
    c = _real_cfg()
    c.agents.aux_models.memory = AuxModelConfig(model="whatever-x", provider="nvidia")
    p = resolve_aux_preset(c, purpose="memory")
    assert (p.model, p.provider) == ("whatever-x", "nvidia")


def test_judge_pair_is_honored_verbatim() -> None:
    c = _real_cfg()
    c.skills.security.llm_judge.model = "some-model"
    c.skills.security.llm_judge.provider = "nvidia"
    p = resolve_aux_preset(c, purpose="judge")
    assert (p.model, p.provider) == ("some-model", "nvidia")


def test_bare_judge_name_autodetects_its_configured_provider() -> None:
    # Default provider is nvidia; the judge names a glm model. The old resolver
    # paired glm with nvidia (404 in production); the name must land on the
    # configured zhipu provider instead.
    c = _real_cfg(model="nemotron-3", provider="nvidia")
    c.providers.nvidia.api_key = "k-nvidia"
    c.skills.security.llm_judge.model = "glm-4.6"
    p = resolve_aux_preset(c, purpose="judge")
    assert (p.model, p.provider) == ("glm-4.6", "zhipu")


def test_foreign_name_falls_back_to_whole_default_preset() -> None:
    # No configured provider serves this name → specific-or-default means the
    # DEFAULT pair, not the foreign name on the default provider.
    c = _real_cfg(model="glm-5.2", provider="zhipu")
    c.skills.security.llm_judge.model = "totally-unknown-model-xyz"
    p = resolve_aux_preset(c, purpose="judge")
    assert (p.model, p.provider) == ("glm-5.2", "zhipu")


def test_dream_override_deprecated_but_autodetected() -> None:
    c = _real_cfg(model="nemotron-3", provider="nvidia")
    c.providers.nvidia.api_key = "k-nvidia"
    c.memory.dream.model_override = "glm-4.6"
    p = resolve_aux_preset(c, purpose="memory")
    assert (p.model, p.provider) == ("glm-4.6", "zhipu")


def test_aux_pair_wins_over_dream_override() -> None:
    c = _real_cfg()
    c.agents.aux_models.memory = AuxModelConfig(model="pair-model", provider="zhipu")
    c.memory.dream.model_override = "override-model"
    assert resolve_aux_preset(c, purpose="memory").model == "pair-model"


def test_never_returns_a_hardcoded_model_for_any_default() -> None:
    c = Config()
    c.agents.defaults.provider = "anthropic"
    c.agents.defaults.model = "claude-x"
    p = resolve_aux_preset(c, purpose="memory")
    assert (p.model, p.provider) == ("claude-x", "anthropic")


def test_loops_returns_none_when_unconfigured() -> None:
    # Unlike memory/judge, loops does NOT fall back to the default preset —
    # None tells the caller to ride the live session model instead.
    assert resolve_aux_preset(_real_cfg(), purpose="loops") is None


def test_loops_aux_pair_is_honored_verbatim() -> None:
    c = _real_cfg()
    c.agents.aux_models.loops = AuxModelConfig(model="loops-model", provider="nvidia")
    p = resolve_aux_preset(c, purpose="loops")
    assert (p.model, p.provider) == ("loops-model", "nvidia")


def test_loops_preset_ref_is_resolved() -> None:
    c = _real_cfg()
    c.model_presets["fast"] = c.resolve_default_preset().model_copy(update={"model": "fast-model"})
    c.agents.aux_models.loops = AuxModelConfig(preset="fast")
    p = resolve_aux_preset(c, purpose="loops")
    assert p.model == "fast-model"
