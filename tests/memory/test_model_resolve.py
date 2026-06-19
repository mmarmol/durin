"""Unit tests for ``durin.memory.model_resolve``."""

from __future__ import annotations

from types import SimpleNamespace

from durin.memory.model_resolve import resolve_memory_model


def _cfg(
    *,
    aux_memory=None,
    dream_override: str | None = None,
    presets: dict | None = None,
):
    """Build a fake DurinConfig-shaped namespace for the resolver."""
    presets = presets or {}

    class _Cfg(SimpleNamespace):
        def resolve_preset(self, name):
            if name not in presets:
                raise KeyError(name)
            return presets[name]

    agents = SimpleNamespace(aux_models=SimpleNamespace(memory=aux_memory))
    memory = SimpleNamespace(
        dream=SimpleNamespace(model_override=dream_override),
    )
    return _Cfg(agents=agents, memory=memory)


def test_returns_none_when_nothing_set():
    cfg = _cfg()
    assert resolve_memory_model(cfg) is None


def test_returns_none_when_config_is_none():
    assert resolve_memory_model(None) is None


def test_falls_back_to_dream_model_override():
    cfg = _cfg(dream_override="glm-4-flash")
    assert resolve_memory_model(cfg) == "glm-4-flash"


def test_aux_memory_inline_model_wins_over_dream_override():
    cfg = _cfg(
        aux_memory=SimpleNamespace(preset=None, model="gpt-4o-mini"),
        dream_override="glm-4-flash",
    )
    assert resolve_memory_model(cfg) == "gpt-4o-mini"


def test_aux_memory_preset_wins_over_inline_model():
    cfg = _cfg(
        aux_memory=SimpleNamespace(preset="cheap", model="ignored-when-preset-set"),
        presets={"cheap": SimpleNamespace(model="glm-4-flash")},
    )
    assert resolve_memory_model(cfg) == "glm-4-flash"


def test_unknown_preset_falls_back_to_inline_model():
    cfg = _cfg(
        aux_memory=SimpleNamespace(preset="missing", model="inline-fallback"),
        presets={},
    )
    assert resolve_memory_model(cfg) == "inline-fallback"


def test_unknown_preset_no_inline_falls_back_to_dream_override():
    cfg = _cfg(
        aux_memory=SimpleNamespace(preset="missing", model=None),
        dream_override="glm-5.1",
        presets={},
    )
    assert resolve_memory_model(cfg) == "glm-5.1"


def test_partial_config_without_aux_models_section():
    """Real-world: older config files don't have aux_models at all."""
    cfg = SimpleNamespace(
        agents=SimpleNamespace(),  # no aux_models attribute
        memory=SimpleNamespace(
            dream=SimpleNamespace(model_override="glm-5.1"),
        ),
    )
    assert resolve_memory_model(cfg) == "glm-5.1"


def test_partial_config_without_memory_dream_section():
    cfg = SimpleNamespace(agents=SimpleNamespace())
    assert resolve_memory_model(cfg) is None


# -- resolve_aux_preset: specific-or-default, never a hardcoded model -----------

from durin.config.schema import AuxModelConfig, Config  # noqa: E402
from durin.memory.model_resolve import resolve_aux_preset  # noqa: E402


def _real_cfg(model="glm-5.2", provider="zai_coding_plan") -> Config:
    c = Config()
    c.agents.defaults.provider = provider
    c.agents.defaults.model = model
    return c


def test_memory_falls_back_to_default_preset_when_unset() -> None:
    p = resolve_aux_preset(_real_cfg(), purpose="memory")
    assert p.model == "glm-5.2"
    assert p.provider == "zai_coding_plan"


def test_judge_falls_back_to_default_preset_when_unset() -> None:
    assert resolve_aux_preset(_real_cfg(), purpose="judge").model == "glm-5.2"


def test_memory_aux_model_takes_precedence() -> None:
    c = _real_cfg()
    c.agents.aux_models.memory = AuxModelConfig(model="glm-4.6", provider="zai_coding_plan")
    assert resolve_aux_preset(c, purpose="memory").model == "glm-4.6"


def test_memory_override_runs_on_default_provider() -> None:
    c = _real_cfg()
    c.memory.dream.model_override = "glm-4.6"
    p = resolve_aux_preset(c, purpose="memory")
    assert p.model == "glm-4.6"
    assert p.provider == "zai_coding_plan"


def test_judge_specific_model_takes_precedence() -> None:
    c = _real_cfg()
    c.skills.security.llm_judge.model = "glm-4.6"
    assert resolve_aux_preset(c, purpose="judge").model == "glm-4.6"


def test_never_returns_glm_5_1_for_non_zai_default() -> None:
    p = resolve_aux_preset(_real_cfg(model="claude-x", provider="anthropic"), purpose="memory")
    assert p.model == "claude-x"
    assert p.provider == "anthropic"
