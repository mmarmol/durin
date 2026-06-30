from types import SimpleNamespace
from unittest.mock import MagicMock

from durin.config.loader import get_config_path, load_config, save_config
from durin.config.schema import PersonaConfig


def test_reload_app_config_picks_up_new_persona(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    cfg = load_config(get_config_path())
    from durin.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)  # construct bare; only app_config matters here
    loop.app_config = cfg
    loop.model_presets = {}
    assert "qa_persona" not in loop.app_config.persona_names()
    # mutate config on disk
    cfg.personas["qa_persona"] = PersonaConfig(soul="default", model=None, description="qa")
    save_config(cfg, get_config_path())
    loop.reload_app_config()
    assert "qa_persona" in loop.app_config.persona_names()


def test_apply_default_model_live_reapplies_new_default(tmp_path, monkeypatch):
    """apply_default_model_live re-reads config and re-resolves the default
    model through set_model_preset (the same path /model uses)."""
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    cfg = load_config(get_config_path())
    from durin.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)  # bare; only the fields below are touched
    loop.app_config = cfg
    loop.model_presets = {}

    applied: list[str] = []
    loop.set_model_preset = lambda name, *, publish_update=True: applied.append(name)

    # set a new default provider+model pair on disk (picker form)
    cfg.agents.defaults.provider = "openai"
    cfg.agents.defaults.model = "gpt-4o-mini"
    save_config(cfg, get_config_path())

    loop.apply_default_model_live()

    # reloaded config picked up the new model
    assert loop.app_config.agents.defaults.model == "gpt-4o-mini"
    # set_model_preset was invoked with the resolved ref (ad-hoc preset keyed by model)
    assert applied == ["gpt-4o-mini"]
    assert "gpt-4o-mini" in loop.model_presets


def _provider(default_model: str) -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = default_model
    provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)
    return provider


def _real_loop_default_only(tmp_path):
    """A genuine AgentLoop (NOT stubbed for ``set_model_preset``) whose only
    registered preset is ``"default"`` — so ``apply_default_model_live`` drives
    the real ``normalize_preset_name`` path that raised KeyError for an
    ``auto`` provider default before the fix."""
    from durin.bus.queue import MessageBus
    from durin.agent.loop import AgentLoop
    from durin.config.schema import ModelPresetConfig
    from durin.providers.factory import ProviderSnapshot

    def loader(name, preset=None):
        target = preset or ModelPresetConfig(model=name)
        return ProviderSnapshot(
            provider=_provider(target.model),
            model=target.model,
            context_window_tokens=target.context_window_tokens,
            signature=("model_preset", name, target.model),
        )

    return AgentLoop(
        bus=MessageBus(),
        provider=_provider("base-model"),
        workspace=tmp_path,
        model="base-model",
        context_window_tokens=1000,
        model_presets={"default": ModelPresetConfig(model="base-model")},
        preset_snapshot_loader=loader,
        app_config=load_config(get_config_path()),
    )


def test_apply_default_model_live_auto_provider_applies_without_keyerror(tmp_path, monkeypatch):
    """Regression: an ``auto`` provider default (the schema default, reachable
    from the settings UI's empty/auto provider picker) must apply live through
    the rebuilt ``"default"`` preset — NOT a synthetic bare-model ref, which
    raised ``KeyError: model_preset '<model>' not found`` out of
    ``on_default_changed``. Drives the REAL ``set_model_preset`` path."""
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    loop = _real_loop_default_only(tmp_path)

    # New default: a model on the auto provider (no explicit provider) — the
    # form the DefaultModelControl picker produces when provider is left empty.
    cfg = loop.app_config
    cfg.agents.defaults.provider = "auto"
    cfg.agents.defaults.model = "anthropic/claude-opus-4-5"
    save_config(cfg, get_config_path())

    loop.apply_default_model_live()  # must NOT raise (KeyError before the fix)

    # The new default was actually applied to the running loop.
    assert loop.app_config.agents.defaults.model == "anthropic/claude-opus-4-5"
    assert loop.model_preset == "default"
    assert loop.model == "anthropic/claude-opus-4-5"
