"""decision_log config keys exist with correct defaults and reach the Consolidator."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from durin.config.schema import AgentDefaults


def test_agent_defaults_decision_log_fields():
    d = AgentDefaults()
    assert d.decision_log_enabled is True
    assert d.decision_log_max_entries == 10
    assert d.decision_log_max_chars == 1500


def test_consolidator_stores_decision_log_config():
    from durin.agent.memory import Consolidator

    cons = Consolidator(
        store=MagicMock(),
        provider=MagicMock(),
        model="m",
        sessions=MagicMock(),
        context_window_tokens=65_536,
        build_messages=MagicMock(),
        get_tool_definitions=MagicMock(),
        decision_log_enabled=False,
        decision_log_max_entries=7,
        decision_log_max_chars=900,
    )
    assert cons.decision_log_enabled is False
    assert cons.decision_log_max_entries == 7
    assert cons.decision_log_max_chars == 900


def test_loop_wires_decision_log_config_into_consolidator(tmp_path):
    from durin.agent.loop import AgentLoop
    from durin.bus.queue import MessageBus

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager") as mock_sub_mgr:
        mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    assert loop.consolidator.decision_log_max_entries == 10
    assert loop.consolidator.decision_log_max_chars == 1500
    assert loop.consolidator.decision_log_enabled is True


def test_from_config_threads_decision_log_to_consolidator(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import patch

    from durin.agent.loop import AgentLoop
    from durin.config.schema import Config

    config = Config.model_validate({
        "agents": {
            "defaults": {
                "model": "openai/gpt-4.1",
                "workspace": str(tmp_path),
                "decision_log_enabled": False,
                "decision_log_max_entries": 25,
                "decision_log_max_chars": 800,
            }
        },
    })
    fake_provider = MagicMock()
    fake_provider.get_default_model.return_value = "openai/gpt-4.1"
    fake_provider.generation = SimpleNamespace(
        max_tokens=4096, temperature=0.1, reasoning_effort=None
    )
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.agent.loop.Consolidator") as mock_consolidator:
        mock_consolidator.return_value = MagicMock()
        AgentLoop.from_config(config)
    _, kwargs = mock_consolidator.call_args
    assert kwargs["decision_log_max_entries"] == 25
    assert kwargs["decision_log_max_chars"] == 800
    assert kwargs["decision_log_enabled"] is False
