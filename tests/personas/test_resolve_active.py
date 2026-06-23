from durin.personas.resolve import resolve_active_persona_name
from durin.config.schema import Config


def test_precedence_cron_over_session_over_default():
    cfg = Config()
    cfg.agents.defaults.persona = "global"
    assert resolve_active_persona_name(cfg, {"persona": "sess"}, "cron") == "cron"
    assert resolve_active_persona_name(cfg, {"persona": "sess"}, None) == "sess"
    assert resolve_active_persona_name(cfg, {}, None) == "global"
    assert resolve_active_persona_name(cfg, None, None) == "global"


def test_none_when_nothing_set():
    cfg = Config()
    assert resolve_active_persona_name(cfg, None, None) is None


def test_process_direct_accepts_persona():
    import inspect
    from durin.agent.loop import AgentLoop

    sig = inspect.signature(AgentLoop.process_direct)
    assert "persona" in sig.parameters
