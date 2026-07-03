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


def test_active_persona_returns_soul_and_model(tmp_path):
    """_active_persona resolves soul body + model_ref from config persona."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from durin.agent.loop import AgentLoop
    from durin.bus.queue import MessageBus
    from durin.config.schema import Config, PersonaConfig
    from durin.souls.store import SoulStore

    soul_body = "You are a helpful test assistant."
    SoulStore(tmp_path).write("default", soul_body)

    cfg = Config()
    cfg.personas["tester"] = PersonaConfig(soul="default", model="test-preset")

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)

    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager") as MockSub:
        MockSub.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=tmp_path,
            model="test-model",
            app_config=cfg,
        )

    session = SimpleNamespace(metadata={"persona": "tester"})
    body, model_ref = loop._active_persona(session, None)

    assert body == soul_body
    assert model_ref == "test-preset"


def _cfg(channels: dict, default: str | None = None):
    from types import SimpleNamespace

    class _Channels:
        pass

    ch = _Channels()
    for name, section in channels.items():
        setattr(ch, name, section)
    return SimpleNamespace(
        channels=ch,
        agents=SimpleNamespace(defaults=SimpleNamespace(persona=default)),
    )


def test_channel_default_persona_applies_to_fresh_sessions():
    cfg = _cfg({"slack": {"persona": "work"}}, default="home")
    assert resolve_active_persona_name(cfg, None, None, channel="slack", chat_id="D1") == "work"


def test_chat_persona_overrides_channel_default():
    cfg = _cfg(
        {"slack": {"persona": "work", "chat_personas": {"C_OPS": "ops"}}}, default="home"
    )
    assert (
        resolve_active_persona_name(cfg, None, None, channel="slack", chat_id="C_OPS") == "ops"
    )
    assert (
        resolve_active_persona_name(cfg, None, None, channel="slack", chat_id="C_OTHER")
        == "work"
    )


def test_session_persona_beats_channel_config():
    cfg = _cfg({"slack": {"persona": "work", "chat_personas": {"C1": "ops"}}}, default="home")
    meta = {"persona": "picked"}
    assert resolve_active_persona_name(cfg, meta, None, channel="slack", chat_id="C1") == "picked"


def test_cron_override_beats_everything():
    cfg = _cfg({"slack": {"persona": "work"}}, default="home")
    assert (
        resolve_active_persona_name(cfg, {"persona": "picked"}, "cronp", channel="slack")
        == "cronp"
    )


def test_channel_without_persona_falls_back_to_global_default():
    cfg = _cfg({"slack": {"enabled": True}}, default="home")
    assert resolve_active_persona_name(cfg, None, None, channel="slack", chat_id="D1") == "home"


def test_unknown_channel_falls_back_to_global_default():
    cfg = _cfg({}, default="home")
    assert resolve_active_persona_name(cfg, None, None, channel="ghost", chat_id="x") == "home"
