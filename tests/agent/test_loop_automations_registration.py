"""AgentLoop.register_automations_tool: the automations tool's late-binding
registration path (durin/agent/loop.py) — the gateway builds the
AutomationsRuntime after this AgentLoop already exists (its closures call
agent.process_direct), so _register_default_tools() never sees it at
__init__ time."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus


def _make_loop(workspace) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop


def test_automations_tool_absent_before_registration(tmp_path):
    loop = _make_loop(tmp_path)
    assert loop.automations_runtime is None
    assert not loop.tools.has("automations")


def test_register_automations_tool_adds_the_tool_and_stores_the_runtime(tmp_path):
    loop = _make_loop(tmp_path)
    runtime = object()  # AutomationsTool.enabled only checks "is not None"

    loop.register_automations_tool(runtime)

    assert loop.automations_runtime is runtime
    assert loop.tools.has("automations")
    assert loop.tools.get("automations").name == "automations"
