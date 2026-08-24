"""AgentLoop.register_automations_tool: the automations tool's late-binding
registration path, added beside register_loops_tool (durin/agent/loop.py) for
the same reason — the gateway builds each runtime after this AgentLoop
already exists, so _register_default_tools() never sees either one at
__init__ time. Loops stays live and unmodified until its own cutover task
retires it."""

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


def test_register_automations_tool_leaves_the_loops_tool_alone(tmp_path):
    """B11 adds the automations tool beside the loops one — it must not
    disturb loops' own (separate) registration path, which stays live until
    a later cutover task retires it."""
    loop = _make_loop(tmp_path)
    loop.register_automations_tool(object())

    assert loop.loops_runtime is None
    assert not loop.tools.has("loops")

    loop.register_loops_tool(object())

    assert loop.tools.has("loops")
    assert loop.tools.has("automations")
