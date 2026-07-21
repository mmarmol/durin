"""Tests for in-node progress: the tool a running workflow node is about to use."""

import time

import pytest

from durin.agent.hook import AgentHookContext
from durin.providers.base import ToolCallRequest
from durin.workflow.node_progress import NodeProgressHook
from durin.workflow.progress import tool_target


def test_tool_target_prefers_path_then_command_then_query():
    assert tool_target({"path": "a.json", "query": "q"}) == "a.json"
    assert tool_target({"command": "ls -la"}) == "ls -la"
    assert tool_target({"query": "GIKEN storage"}) == "GIKEN storage"
    assert tool_target({"unknown": "x"}) is None
    assert tool_target({}) is None


def test_tool_target_is_capped():
    assert len(tool_target({"command": "x" * 500})) == 120


@pytest.mark.asyncio
async def test_before_execute_tools_emits_the_tool_about_to_run():
    seen = []
    hook = NodeProgressHook(seen.append)
    ctx = AgentHookContext(iteration=3, messages=[])
    ctx.tool_calls = [ToolCallRequest(id="1", name="read_file", arguments={"path": "investigation.json"})]

    await hook.before_execute_tools(ctx)

    assert seen == [{
        "round": 4,
        "activity": {"tool": "read_file", "target": "investigation.json", "at": seen[0]["activity"]["at"]},
        "max_rounds": None,
    }]
    assert isinstance(seen[0]["activity"]["at"], float)


@pytest.mark.asyncio
async def test_before_execute_tools_with_no_tool_calls_emits_no_activity():
    seen = []
    hook = NodeProgressHook(seen.append)
    ctx = AgentHookContext(iteration=0, messages=[])
    ctx.tool_calls = []

    await hook.before_execute_tools(ctx)

    assert seen == [{"round": 1, "activity": None, "max_rounds": None}]


@pytest.mark.asyncio
async def test_before_execute_tools_with_several_calls_reports_only_the_first():
    """Pins the current contract: a round that fans out several tool calls at once
    still reports just the first one. A refactor that emitted all of them should
    fail this test loudly rather than silently changing the wire shape."""
    seen = []
    hook = NodeProgressHook(seen.append)
    ctx = AgentHookContext(iteration=0, messages=[])
    ctx.tool_calls = [
        ToolCallRequest(id="1", name="read_file", arguments={"path": "a.json"}),
        ToolCallRequest(id="2", name="exec", arguments={"command": "ls"}),
    ]

    await hook.before_execute_tools(ctx)

    assert seen == [{
        "round": 1,
        "activity": {"tool": "read_file", "target": "a.json", "at": seen[0]["activity"]["at"]},
        "max_rounds": None,
    }]


@pytest.mark.asyncio
async def test_after_iteration_advances_the_round_without_activity():
    seen = []
    hook = NodeProgressHook(seen.append)
    await hook.after_iteration(AgentHookContext(iteration=4, messages=[]))
    assert seen == [{"round": 5, "activity": None, "max_rounds": None}]


@pytest.mark.asyncio
async def test_first_round_is_reported_as_one_not_zero():
    """The runner counts iterations from zero; a surface rendering the raw value
    would show "round 0 of 10" on a node's first round."""
    seen = []
    hook = NodeProgressHook(seen.append)
    await hook.after_iteration(AgentHookContext(iteration=0, messages=[]))
    assert seen == [{"round": 1, "activity": None, "max_rounds": None}]


@pytest.mark.asyncio
async def test_hook_includes_the_round_budget_in_every_payload():
    """max_rounds is the denominator round is rendered against — it has to ride
    along on both hook methods, not just one, or a surface reading whichever
    frame arrived last could find it missing."""
    seen = []
    hook = NodeProgressHook(seen.append, max_rounds=10)
    ctx = AgentHookContext(iteration=0, messages=[])
    ctx.tool_calls = [ToolCallRequest(id="1", name="read_file", arguments={"path": "x.json"})]

    await hook.before_execute_tools(ctx)
    await hook.after_iteration(ctx)

    assert [p["max_rounds"] for p in seen] == [10, 10]


@pytest.mark.asyncio
async def test_a_raising_emit_never_escapes_the_hook():
    def boom(_payload):
        raise RuntimeError("panel gone")

    hook = NodeProgressHook(boom)
    ctx = AgentHookContext(iteration=1, messages=[])
    ctx.tool_calls = [ToolCallRequest(id="1", name="exec", arguments={"command": "ls"})]
    await hook.before_execute_tools(ctx)   # must not raise
    await hook.after_iteration(ctx)        # must not raise


def _one_node_workflow():
    from durin.workflow.spec import parse_workflow

    return parse_workflow({
        "name": "wf",
        "start": "a",
        "nodes": [{"id": "a", "kind": "work", "title": "Alpha", "prompt": "do it", "next": None}],
    })


def test_engine_reemits_a_full_frame_set_when_a_node_reports(tmp_path):
    """The node reports raw state; the engine turns it into frames. Without this
    the panel would receive a fragment it cannot merge into its node list."""
    from durin.workflow.engine import NodeRunResponse, WorkflowEngine

    wf = _one_node_workflow()
    emitted = []

    def _runner(req):
        req.progress({"round": 2, "activity": {"tool": "read_file", "target": "x.json", "at": 1.0}})
        return NodeRunResponse(output="done", session_key="s", messages=[])

    engine = WorkflowEngine(node_runner=_runner, workspace=str(tmp_path),
                            progress_emit=emitted.append)
    engine.run(wf, "task")

    live = [f for p in emitted for f in p["nodes"] if f["status"] == "running"]
    reported = [f for f in live if f.get("activity")]
    assert reported, f"no frame carried activity; got {[f.get('activity') for f in live]}"
    assert reported[-1]["activity"]["tool"] == "read_file"
    assert reported[-1]["round"] == 2
    assert reported[-1]["started_at"] is not None


def test_engine_frame_reports_the_round_budget(tmp_path):
    """max_rounds is the round budget `round` is measured against — a different
    axis from `budget` (the node's visit budget, how many times the graph may
    re-enter it). Surfaces rendering "round N of M" need this on the frame, or
    they would have to mix the two axes and show the wrong denominator."""
    from durin.workflow.engine import NodeRunResponse, WorkflowEngine

    wf = _one_node_workflow()
    emitted = []

    def _runner(req):
        req.progress({"round": 2, "activity": None, "max_rounds": 10})
        return NodeRunResponse(output="done", session_key="s", messages=[])

    engine = WorkflowEngine(node_runner=_runner, workspace=str(tmp_path),
                            progress_emit=emitted.append)
    engine.run(wf, "task")

    live = [f for p in emitted for f in p["nodes"] if f["status"] == "running"]
    reported = [f for f in live if f.get("round") == 2]
    assert reported, f"no frame carried round 2; got {[f.get('round') for f in live]}"
    assert reported[-1]["max_rounds"] == 10


def test_progress_crosses_the_node_loop_to_the_gateway_loop_without_waiting(tmp_path):
    """The threading contract, exercised in its real shape.

    The engine walks on a worker thread and the node drives its agent turn in a
    nested event loop on that thread, while the gateway's loop runs elsewhere.
    Two things must hold, and neither shows up in a single-loop test: the hook's
    emit must reach the gateway loop, and the node must never wait on it. Here
    every marshalled payload is parked on the gateway loop until the run is over,
    so an emit that waited for it would deadlock the run rather than finish.
    """
    import asyncio
    import threading

    from durin.workflow.engine import NodeRunResponse, WorkflowEngine

    gateway_loop = asyncio.new_event_loop()
    threading.Thread(target=gateway_loop.run_forever, daemon=True).start()
    received: list[dict] = []

    async def _make_gate():
        return asyncio.Event()

    gate = asyncio.run_coroutine_threadsafe(_make_gate(), gateway_loop).result(timeout=5)

    async def _receive(payload):
        await gate.wait()          # the gateway is busy until the run has ended
        received.append(payload)

    def _emit(payload):
        # Mirrors the gateway's own progress emitter: synchronous, marshals onto
        # the main loop, never waits for the result.
        asyncio.run_coroutine_threadsafe(_receive(payload), gateway_loop)

    node_loops: list[object] = []

    def _runner(req):
        async def _turn():
            node_loops.append(asyncio.get_running_loop())
            hook = NodeProgressHook(req.progress)
            ctx = AgentHookContext(iteration=1, messages=[])
            ctx.tool_calls = [ToolCallRequest(id="1", name="read_file", arguments={"path": "x.json"})]
            await hook.before_execute_tools(ctx)

        asyncio.run(_turn())       # the node's own loop, as the real node runner does
        return NodeRunResponse(output="done", session_key="s", messages=[])

    engine = WorkflowEngine(node_runner=_runner, workspace=str(tmp_path), progress_emit=_emit)
    walker = threading.Thread(target=lambda: engine.run(_one_node_workflow(), "task"))
    walker.start()
    walker.join(timeout=10)
    assert not walker.is_alive(), "the run never finished — the node waited on the gateway loop"

    gateway_loop.call_soon_threadsafe(gate.set)
    deadline = time.time() + 5
    while not received and time.time() < deadline:
        time.sleep(0.01)
    gateway_loop.call_soon_threadsafe(gateway_loop.stop)

    assert node_loops and node_loops[0] is not gateway_loop, "the node ran on the gateway's loop"
    activity = [f for p in received for f in p["nodes"] if f.get("activity")]
    assert activity, "the node's activity never reached the gateway loop"
    assert activity[-1]["activity"]["tool"] == "read_file"
