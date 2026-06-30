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
