"""A workflow node's run answers to the node's mode like a chat turn does.

The node's registry is narrowed by its mode, and an ``execute_code`` script
runs its calls through tool instances of its own, outside that registry. The
node runner hands the runner the node's mode too (with the engine's own
verdict and delivery tools admitted), so the call-time gate, the narrowed
schemas and the tool-call mode a script's calls read all apply inside a
node.

Every test drives the real ``AgentNodeRunner`` with a scripted provider that
answers as a model would and records what each request offered.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import threading
from typing import Any

import pytest

from durin.agent.agent_mode import (
    _REGISTRY,
    AgentMode,
    node_gate_mode,
    refuse_tool_call,
    register_mode,
)
from durin.agent.runner import AgentRunner
from durin.agent.tools.base import Tool
from durin.agent.tools.filesystem import WriteFileTool
from durin.agent.tools.mcp_deferral import maybe_defer_mcp_tools
from durin.agent.tools.registry import ToolRegistry
from durin.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from durin.session.manager import SessionManager
from durin.workflow.engine import NodeRunRequest
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import WorkNode, parse_workflow

SCRIPT_READ = AgentMode(
    name="script-read", description="execute_code and reads, no writes",
    allowed=frozenset({"execute_code", "read_file"}),
)
CREATE = "mcp_tracker_create_ticket"
LIST = "mcp_tracker_list_tickets"
TRACKER_CREATE_ONLY = AgentMode(
    name="tracker-create", description="one MCP tool only",
    allowed=frozenset({CREATE}),
)
DENY_ROUTE = AgentMode(
    name="no-route", description="a denylist naming the verdict tool",
    denied=frozenset({"route", "write_file"}),
)
_MODES = (SCRIPT_READ, TRACKER_CREATE_ONLY, DENY_ROUTE)

_SCRIPT = (
    "from durin_tools import read_file, write_file\n"
    "try:\n"
    "    write_file('out.txt', 'written')\n"
    "    print('WROTE')\n"
    "except RuntimeError as e:\n"
    "    print('REFUSED:', e)\n"
    "print('AFTER', read_file('data.txt').strip())\n"
)


@pytest.fixture(autouse=True)
def _custom_modes():
    for mode in _MODES:
        register_mode(mode)
    yield
    for mode in _MODES:
        _REGISTRY.pop(mode.name, None)


@pytest.fixture
def writes(monkeypatch):
    """Every call that reaches the real write tool's ``execute``."""
    calls: list[dict[str, Any]] = []
    original = WriteFileTool.execute

    async def recording(self, **kwargs):
        calls.append(dict(kwargs))
        return await original(self, **kwargs)

    monkeypatch.setattr(WriteFileTool, "execute", recording)
    return calls


class _Scripted(LLMProvider):
    """Answers each work-loop request with the next scripted tool calls (or a
    final text once the script runs out), and a forced call with the tool it
    names. Records the tool list and messages of every request."""

    def __init__(self, rounds: list[list[ToolCallRequest]], final: str,
                 forced_args: dict[str, dict] | None = None) -> None:
        super().__init__()
        self.rounds = list(rounds)
        self.final = final
        self.forced_args = forced_args or {}
        self.loop: list[tuple[list[dict], list[dict]]] = []
        self.forced: list[list[dict]] = []

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, messages, tools=None, tool_choice=None, **kwargs):
        if isinstance(tool_choice, dict):
            name = tool_choice["function"]["name"]
            self.forced.append(list(tools or []))
            return LLMResponse(content=None, finish_reason="tool_calls", tool_calls=[
                ToolCallRequest(id=f"f{len(self.forced)}", name=name,
                                arguments=self.forced_args[name])])
        self.loop.append((list(tools or []), list(messages)))
        if self.rounds:
            return LLMResponse(content="", finish_reason="tool_calls", tool_calls=self.rounds.pop(0))
        return LLMResponse(content=self.final)


def _names(tools: list[dict]) -> set[str]:
    return {t["function"]["name"] for t in tools}


def _description(tools: list[dict], name: str) -> str:
    return next(t["function"]["description"] for t in tools if t["function"]["name"] == name)


def _tool_results(messages: list[dict]) -> list[str]:
    return [str(m.get("content")) for m in messages if m.get("role") == "tool"]


def _run(tmp_path, node, model: _Scripted, cancel_check=None, **runner_kwargs):
    (tmp_path / "data.txt").write_text("data-contents\n", encoding="utf-8")
    nr = AgentNodeRunner(AgentRunner(model), SessionManager(workspace=tmp_path),
                         default_model="test-model", **runner_kwargs)
    return nr(NodeRunRequest(node=node, task="t", upstream_output=None, shared_context=[],
                             run_id="r1", iteration=1, root_session_key=None,
                             cancel_check=cancel_check))


def _routing_node(mode: str) -> WorkNode:
    wf = parse_workflow({"name": "w", "start": "g", "nodes": [
        {"id": "g", "kind": "work", "tools": "default", "mode": mode, "prompt": "judge it",
         "cases": {"PASS": None, "FAIL": None}},
    ]})
    return wf.nodes["g"]


@pytest.mark.skipif(sys.platform == "win32", reason="execute_code is UDS-only")
@pytest.mark.parametrize(
    "cancel_check", [None, lambda: False], ids=["direct", "watched-thread"],
)
def test_a_scripts_write_is_refused_by_the_nodes_mode_and_its_verdict_still_works(
    tmp_path, writes, cancel_check,
):
    """The engine hands a work node a cancel poll, which runs its turn on a
    watched thread; the gate holds on that path and on the direct one."""
    model = _Scripted([
        [ToolCallRequest(id="w1", name="write_file",
                         arguments={"path": "direct.txt", "content": "x"})],
        [ToolCallRequest(id="x1", name="execute_code", arguments={"code": _SCRIPT})],
        [ToolCallRequest(id="r1", name="route",
                         arguments={"label": "PASS", "reason": "checked"})],
    ], final="done")

    resp = _run(tmp_path, _routing_node("script-read"), model, cancel_check=cancel_check)

    assert writes == [], "the write tool must never run in this node"
    assert not list(tmp_path.rglob("out.txt")) and not list(tmp_path.rglob("direct.txt"))
    results = _tool_results(model.loop[-1][1])
    refusal = (
        "Error: Tool 'write_file' is not available to this step in mode 'script-read'. "
        "Tools available to this step: execute_code, read_file, route. "
        "Do not retry it — finish the step with the tools you have."
    )
    # Both paths read the unattended wording: nobody is there to switch modes.
    assert results[0].startswith(refusal)
    script = json.loads(results[1])
    assert script["status"] == "success", script
    assert f"REFUSED: {refusal}\n" in script["output"]
    for text in (results[0], script["output"]):
        assert "/build" not in text and "user" not in text.lower()
    assert "AFTER data-contents" in script["output"]
    # The verdict tool the engine adds is not something the mode can remove.
    assert resp.route_label == "PASS"
    # The model is offered only what the mode allows, plus the verdict tool,
    # and execute_code names only the script tools the mode allows.
    offered = model.loop[0][0]
    assert _names(offered) == {"execute_code", "read_file", "route"}
    assert "write_file" not in _description(offered, "execute_code")
    assert "read_file" in _description(offered, "execute_code")


@pytest.mark.skipif(sys.platform == "win32", reason="execute_code is UDS-only")
def test_a_forced_verdict_call_sends_the_loops_own_tool_list(tmp_path):
    """No `route` call in the loop: the forced call that asks for it sends the
    tool list the loop sent, narrowed descriptions included."""
    model = _Scripted([], final="PASS", forced_args={"route": {"label": "PASS"}})

    resp = _run(tmp_path, _routing_node("script-read"), model)

    assert resp.route_label == "PASS"
    assert model.forced == [model.loop[-1][0]]


@pytest.mark.skipif(sys.platform == "win32", reason="execute_code is UDS-only")
def test_a_build_node_is_unchanged(tmp_path, writes):
    model = _Scripted([
        [ToolCallRequest(id="x1", name="execute_code", arguments={"code": _SCRIPT})],
    ], final="done")

    resp = _run(tmp_path, WorkNode(id="b", tools="default", prompt="do it", next=None), model)

    assert resp.output == "done"
    script = json.loads(_tool_results(model.loop[-1][1])[0])
    assert script["output"] == "WROTE\nAFTER data-contents\n"
    assert len(writes) == 1
    offered = model.loop[0][0]
    assert {"execute_code", "read_file", "write_file", "exec"} <= _names(offered)
    assert "write_file" in _description(offered, "execute_code")


def test_a_read_routing_node_keeps_its_reads_and_verdict(tmp_path, writes):
    model = _Scripted([
        [ToolCallRequest(id="r1", name="read_file", arguments={"path": "data.txt"})],
        [ToolCallRequest(id="w1", name="write_file",
                         arguments={"path": "out.txt", "content": "x"})],
    ], final="FAIL", forced_args={"route": {"label": "FAIL"}})

    resp = _run(tmp_path, _routing_node("read"), model)

    assert resp.route_label == "FAIL"
    results = _tool_results(model.loop[-1][1])
    assert "data-contents" in results[0]
    # The registry never held write_file; the mode gate answers first, naming
    # what the step does have.
    assert results[1].startswith(
        "Error: Tool 'write_file' is not available to this step in mode 'read'. "
        "Tools available to this step: ")
    assert "read_file" in results[1] and "route" in results[1]
    assert "/build" not in results[1] and "user" not in results[1].lower()
    assert writes == []
    offered = _names(model.loop[0][0])
    assert {"read_file", "route"} <= offered
    assert not offered & {"write_file", "exec", "execute_code"}
    assert model.forced == [model.loop[-1][0]]


def test_a_mode_denying_the_verdict_tool_by_name_does_not_remove_it(tmp_path, writes):
    """The engine adds `route` after the mode has filtered the registry, so a
    mode never removed it; gating the run by the mode keeps that meaning."""
    model = _Scripted([
        [ToolCallRequest(id="w1", name="write_file",
                         arguments={"path": "out.txt", "content": "x"})],
        [ToolCallRequest(id="r1", name="route", arguments={"label": "PASS"})],
    ], final="done")

    resp = _run(tmp_path, _routing_node("no-route"), model)

    assert resp.route_label == "PASS"
    assert "route" in _names(model.loop[0][0])
    assert "write_file" not in _names(model.loop[0][0])
    assert writes == []


def test_a_delivering_node_in_a_restricted_mode_still_delivers(tmp_path):
    schema = {"type": "object", "required": ["summary"],
              "properties": {"summary": {"type": "string"}}}
    model = _Scripted([
        [ToolCallRequest(id="d1", name="deliver", arguments={"summary": "fine"})],
    ], final="delivered")

    resp = _run(tmp_path, WorkNode(id="d", tools="default", mode="script-read",
                                   output_schema=schema, next=None), model)

    assert json.loads(resp.output) == {"summary": "fine"}
    assert "deliver" in _names(model.loop[0][0])


class _TrackerTool(Tool):
    """An MCP tool as the gateway registers one: not plugin-discoverable,
    named ``mcp_<server>_<tool>``, and served on the gateway's loop."""

    _plugin_discoverable = False

    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} in the tracker"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        self._calls.append(self._name)
        return f"{self._name} done"


def test_a_node_reaches_only_the_mcp_tools_its_mode_names(tmp_path):
    """The gateway's registry has its MCP tools deferred behind the bridges.
    A node selecting that server in a mode naming one of them is offered that
    one tool, runs it, and is refused the other, directly or by bridge."""
    calls: list[str] = []
    live = ToolRegistry()
    live.register(_TrackerTool(CREATE, calls))
    live.register(_TrackerTool(LIST, calls))
    assert maybe_defer_mcp_tools(live, type("C", (), {"enabled": True, "threshold_tokens": 1})()) == 2

    owner = asyncio.new_event_loop()
    thread = threading.Thread(target=owner.run_forever, daemon=True)
    thread.start()
    try:
        model = _Scripted([
            [ToolCallRequest(id="c1", name=CREATE, arguments={})],
            [ToolCallRequest(id="c2", name=LIST, arguments={})],
            [ToolCallRequest(id="c3", name="mcp_invoke",
                             arguments={"name": LIST, "arguments": {}})],
        ], final="done")
        resp = _run(tmp_path, WorkNode(id="m", mcps=("tracker",), mode="tracker-create",
                                       prompt="file it", next=None),
                    model, live_tool_registry=live, main_loop=owner)
    finally:
        owner.call_soon_threadsafe(owner.stop)
        thread.join()
        owner.close()

    assert resp.output == "done"
    assert _names(model.loop[0][0]) == {CREATE}
    assert calls == [CREATE]
    results = _tool_results(model.loop[-1][1])
    available = f"Tools available to this step: {CREATE}."
    assert results[1].startswith(
        f"Error: Tool '{LIST}' is not available to this step in mode 'tracker-create'. "
        + available)
    assert results[2].startswith(
        "Error: Tool 'mcp_invoke' is not available to this step in mode 'tracker-create'. "
        + available)


def test_a_conversation_refusal_keeps_its_wording():
    """Modes applied to a conversation are attended: the refusal is worded
    exactly as it always was."""
    mode = AgentMode(name="script-read", description="", allowed=frozenset({"read_file"}))

    assert refuse_tool_call(mode, "write_file") == (
        "Error: Tool 'write_file' is not available in mode 'script-read'. "
        "The user must run `/build` (or the active mode must change) before this "
        "tool can be called. Do not retry — adjust your approach."
    )


def test_an_unattended_denylist_refusal_names_no_tool_list():
    mode = node_gate_mode(DENY_ROUTE, kept=["read_file"], engine_tools=["route"])

    assert refuse_tool_call(mode, "write_file") == (
        "Error: Tool 'write_file' is not available to this step in mode 'no-route'. "
        "Do not retry it — finish the step with the tools you have."
    )


# --- The first request a node sends is what it was before nodes got a mode ---


def _without_mode(monkeypatch):
    """Reproduce the node runner before it put the node's mode on its run
    specs: the runner sees no mode, for its runs and for the tool list the
    forced calls send."""
    run = AgentRunner.run
    definitions = AgentRunner._active_tool_definitions

    async def run_without_mode(self, spec):
        return await run(self, dataclasses.replace(spec, mode_provider=None))

    monkeypatch.setattr(AgentRunner, "run", run_without_mode)
    monkeypatch.setattr(AgentRunner, "_active_tool_definitions", staticmethod(
        lambda spec: definitions(dataclasses.replace(spec, mode_provider=None))))


def _requests(model: _Scripted) -> list[tuple[list[dict], list[dict] | None]]:
    """Every request's tool list and messages. A message's ``timestamp`` is the
    wall-clock time it was stored, which no provider sends and which differs
    between any two runs; every other field is compared."""
    loop = [
        (tools, [{k: v for k, v in m.items() if k != "timestamp"} for m in messages])
        for tools, messages in model.loop
    ]
    return [*loop, *((tools, None) for tools in model.forced)]


@pytest.mark.parametrize("routing", [True, False], ids=["routing", "plain"])
@pytest.mark.parametrize("mode", ["build", "read"])
def test_a_build_or_read_node_sends_what_it_sent_before(tmp_path, monkeypatch, mode, routing):
    def node() -> WorkNode:
        if routing:
            return _routing_node(mode)
        return WorkNode(id="g", tools="default", mode=mode, prompt="look into it", next=None)

    def model() -> _Scripted:
        return _Scripted(
            [[ToolCallRequest(id="r1", name="read_file", arguments={"path": "data.txt"})]],
            final="PASS", forced_args={"route": {"label": "PASS"}},
        )

    current = model()
    _run(tmp_path, node(), current)
    _without_mode(monkeypatch)
    before = model()
    _run(tmp_path, node(), before)

    assert len(current.loop) == len(before.loop) == 2
    assert len(current.forced) == len(before.forced) == (1 if routing else 0)
    assert _requests(current) == _requests(before)
