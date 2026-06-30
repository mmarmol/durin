import os
from durin.config.loader import load_config, save_config, get_config_path
from durin.config.schema import Config, PersonaConfig


def test_reload_app_config_picks_up_new_persona(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    cfg = load_config(get_config_path())
    from durin.agent.loop import AgentLoop
    loop = AgentLoop.__new__(AgentLoop)          # construct bare; only app_config matters here
    loop.app_config = cfg
    loop.model_presets = {}
    assert "qa_persona" not in loop.app_config.persona_names()
    # mutate config on disk
    cfg.personas["qa_persona"] = PersonaConfig(soul="default", model=None, description="qa")
    save_config(cfg, get_config_path())
    loop.reload_app_config()
    assert "qa_persona" in loop.app_config.persona_names()
