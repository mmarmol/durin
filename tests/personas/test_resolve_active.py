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
