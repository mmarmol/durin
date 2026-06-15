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
         patch("durin.agent.loop.SubagentManager") as MockSubMgr:
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    assert loop.consolidator.decision_log_max_entries == 10
    assert loop.consolidator.decision_log_max_chars == 1500
    assert loop.consolidator.decision_log_enabled is True
