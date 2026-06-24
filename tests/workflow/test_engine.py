"""Tests for the sequential flow-graph engine (graph logic, mocked node runner)."""

from pathlib import Path

import pytest

from durin.workflow.condition import CommandOutcome
from durin.workflow.engine import NodeRunRequest, NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def _engine(node_outputs, command_results):
    """Engine with a scripted node runner + scripted command results.

    node_outputs: dict node_id -> output string.
    command_results: list of bool (pass/fail), consumed in decision order.
    """
    calls = []

    def node_runner(req: NodeRunRequest) -> NodeRunResponse:
        calls.append(req)
        return NodeRunResponse(
            output=node_outputs[req.node.id],
            session_key=f"workflow:{req.run_id}:{req.node.id}:{req.iteration}",
            messages=[{"role": "assistant", "content": node_outputs[req.node.id]}],
        )

    results = iter(command_results)

    def command_runner(command, *, cwd=None, timeout=30):
        return CommandOutcome(passed=next(results), exit_code=0, output="")

    eng = WorkflowEngine(
        node_runner=node_runner,
        run_id_factory=lambda: "r1",
        command_runner=command_runner,
    )
    return eng, calls


def test_linear_two_nodes_complete():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"}, [])
    res = eng.run(wf, "do it")
    assert res.status == "completed"
    assert res.final_output == "out-b"
    assert [r.node_id for r in res.runs] == ["a", "b"]
    assert res.runs[0].session_key == "workflow:r1:a:1"


def test_output_passes_downstream():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"}, [])
    eng.run(wf, "do it")
    # b received a's output as upstream_output
    b_call = [c for c in calls if c.node.id == "b"][0]
    assert b_call.upstream_output == "out-a"


def test_decision_pass_continues():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "decision", "command": "x", "on_pass": "b", "on_fail": "a"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"}, [True])
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert [r.node_id for r in res.runs] == ["a", "gate", "b"]
    gate_run = [r for r in res.runs if r.node_id == "gate"][0]
    assert gate_run.passed is True
    b_call = [c for c in calls if c.node.id == "b"][0]
    assert b_call.upstream_output == "out-a"


def test_decision_fail_loops_back_then_passes():
    wf = parse_workflow({
        "name": "d", "start": "a", "max_visits": 3,
        "nodes": [
            {"id": "a", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "decision", "command": "x", "on_pass": None, "on_fail": "a"},
        ],
    })
    eng, _ = _engine({"a": "out-a"}, [False, True])  # fail once, then pass
    res = eng.run(wf, "t")
    assert res.status == "completed"
    # a runs twice (iteration 1, then 2 after loop-back), gate twice
    assert [r.node_id for r in res.runs] == ["a", "gate", "a", "gate"]
    a_runs = [r for r in res.runs if r.node_id == "a"]
    assert [r.iteration for r in a_runs] == [1, 2]


def test_command_decision_runs_in_configured_cwd():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "decision", "command": "x", "on_pass": None, "on_fail": "a"},
        ],
    })
    seen_cwd = []

    def node_runner(req):
        return NodeRunResponse(output="out", session_key=None, messages=[])

    def command_runner(command, *, cwd=None, timeout=30):
        seen_cwd.append(cwd)
        return CommandOutcome(passed=True, exit_code=0, output="")

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                         command_runner=command_runner, command_cwd="/ws")
    eng.run(wf, "t")
    # the command gate runs in the workflow's workspace, not the engine's process cwd,
    # so a command can see files the work nodes wrote.
    assert seen_cwd == ["/ws"]


def test_max_visits_aborts_infinite_loop():
    wf = parse_workflow({
        "name": "d", "start": "a", "max_visits": 2,
        "nodes": [
            {"id": "a", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "decision", "command": "x", "on_pass": None, "on_fail": "a"},
        ],
    })
    eng, _ = _engine({"a": "out-a"}, [False, False, False, False])  # never passes
    res = eng.run(wf, "t")
    assert res.status == "max_visits"


def test_shared_vs_own_context():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "context": "shared", "next": "b"},
            {"id": "b", "kind": "work", "context": "own", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"}, [])
    eng.run(wf, "t")
    a_call = [c for c in calls if c.node.id == "a"][0]
    b_call = [c for c in calls if c.node.id == "b"][0]
    # a (shared) starts with empty shared context; b (own) also sees no shared
    # context but a's message was appended to the shared buffer after a ran, so
    # b — being 'own' — must NOT receive it.
    assert a_call.shared_context == []
    assert b_call.shared_context == []   # own node: isolated from the shared buffer


def test_shared_buffer_accumulates_across_shared_nodes():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "context": "shared", "next": "b"},
            {"id": "b", "kind": "work", "context": "shared", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"}, [])
    eng.run(wf, "t")
    b_call = [c for c in calls if c.node.id == "b"][0]
    # b is the second shared node: it must receive a's appended message
    assert b_call.shared_context == [{"role": "assistant", "content": "out-a"}]


def test_agent_routing_node_routes_on_its_own_verdict():
    """A routing work node (with on_pass/on_fail) runs as an agent and routes on its
    own PASS/FAIL output — no external judge runner needed."""
    seq = []

    def runner(req):
        seq.append(req.node.id)
        if req.node.id == "gate":
            # fail on first visit, pass on second
            output = "PASS\nok" if seq.count("gate") > 1 else "FAIL\nfix it"
            return NodeRunResponse(output=output)
        return NodeRunResponse(output={"prod": "draft v1", "done": "done-out"}[req.node.id])

    wf = parse_workflow({"name": "w", "start": "prod", "nodes": [
        {"id": "prod", "kind": "work", "next": "gate"},
        {"id": "gate", "kind": "work", "prompt": "good?", "on_pass": "done", "on_fail": "prod"},
        {"id": "done", "kind": "work", "next": None},
    ]})
    res = WorkflowEngine(runner).run(wf, "task")
    assert res.status == "completed"
    assert seq == ["prod", "gate", "prod", "gate", "done"]   # looped once on FAIL


def test_agent_routing_node_fail_threads_feedback():
    """On FAIL, the routing node's output is threaded into the upstream for the next run."""
    seen_inputs = []

    def runner(req):
        if req.node.id == "prod":
            seen_inputs.append(req.upstream_output)
            return NodeRunResponse(output="attempt")
        # gate: fail first, then pass
        output = "FAIL\nneed more detail" if seen_inputs and seen_inputs[-1] is None else "PASS"
        return NodeRunResponse(output=output)

    wf = parse_workflow({"name": "w", "start": "prod", "max_visits": 3, "nodes": [
        {"id": "prod", "kind": "work", "next": "gate"},
        {"id": "gate", "kind": "work", "prompt": "eval", "on_pass": None, "on_fail": "prod"},
    ]})
    WorkflowEngine(runner).run(wf, "t")
    # second run of 'prod' must have received reviewer feedback in its upstream_output
    assert any(inp and "need more detail" in inp for inp in seen_inputs)


def test_command_routing_node_routes_on_exit_code():
    """A command routing node (is_command=True) routes on the command's exit code."""
    def runner(req):
        return NodeRunResponse(output="built")

    def cmd(command, cwd=None):
        return CommandOutcome(passed=True, output="ok", exit_code=0)

    wf = parse_workflow({"name": "w", "start": "prod", "nodes": [
        {"id": "prod", "kind": "work", "next": "gate"},
        {"id": "gate", "kind": "decision", "command": "true", "on_pass": "done", "on_fail": "prod"},
        {"id": "done", "kind": "work"},
    ]})
    res = WorkflowEngine(runner, command_runner=cmd).run(wf, "t")
    assert res.status == "completed"


def test_subworkflow_node_runs_and_threads_output():
    from durin.workflow.spec import parse_workflow as _pw
    wf = _pw({"name": "d", "start": "sub", "nodes": [
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": "after"},
        {"id": "after", "kind": "work", "next": None},
    ]})
    calls = []

    def subworkflow_runner(name, task, root_session_key=None):
        calls.append((name, task, root_session_key))
        return "child-output"

    seen = []

    def node_runner(req):
        seen.append(req.upstream_output)
        return NodeRunResponse(output="after-out", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                         subworkflow_runner=subworkflow_runner)
    res = eng.run(wf, "the task", root_session_key="conv:1")
    assert res.status == "completed"
    # the sub-workflow is invoked with the run's root session key, so its nested
    # node sessions anchor to the invoking conversation (no orphan subtrees).
    assert calls == [("child", "the task", "conv:1")]
    # the work node after the subworkflow saw the child's output as upstream
    assert "child-output" in seen


def test_subworkflow_node_without_runner_raises():
    from durin.workflow.spec import parse_workflow as _pw
    wf = _pw({"name": "d", "start": "sub", "nodes": [
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": None},
    ]})

    def node_runner(req):
        return NodeRunResponse(output="x", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")
    with pytest.raises(RuntimeError, match="subworkflow"):
        eng.run(wf, "t")


def test_parallel_runs_all_branches_and_merges():
    from durin.workflow.spec import parse_workflow as _pw
    wf = _pw({"name": "d", "start": "fan", "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["a", "b", "c"], "next": "join"},
        {"id": "a", "kind": "work"},
        {"id": "b", "kind": "work"},
        {"id": "c", "kind": "work"},
        {"id": "join", "kind": "work", "next": None},
    ]})
    outputs = {"a": "out-A", "b": "out-B", "c": "out-C", "join": "joined"}
    seen_inputs = []

    def node_runner(req):
        if req.node.id == "join":
            seen_inputs.append(req.upstream_output)
        return NodeRunResponse(output=outputs[req.node.id], session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")
    res = eng.run(wf, "the task")
    assert res.status == "completed"
    # the join node received the merged output of all three branches
    merged = seen_inputs[0]
    assert "out-A" in merged and "out-B" in merged and "out-C" in merged
    # the parallel node recorded a run whose output is the merge
    fan_run = [r for r in res.runs if r.node_id == "fan"][0]
    assert "out-A" in fan_run.output and "out-B" in fan_run.output and "out-C" in fan_run.output


def test_parallel_branches_get_the_parallel_input():
    from durin.workflow.spec import parse_workflow as _pw
    wf = _pw({"name": "d", "start": "pre", "nodes": [
        {"id": "pre", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches": ["a"], "next": None},
        {"id": "a", "kind": "work"},
    ]})
    seen = {}

    def node_runner(req):
        seen[req.node.id] = req.upstream_output
        return NodeRunResponse(output=f"{req.node.id}-out", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")
    eng.run(wf, "t")
    # branch 'a' saw 'pre's output (the input flowing into the parallel node)
    assert seen["a"] == "pre-out"


def _writing_wf(reconcile, **extra):
    nodes = [
        {"id": "fan", "kind": "parallel", "branches": ["a", "b"],
         "reconcile": reconcile, "next": None, **extra},
        {"id": "a", "kind": "work", "tools": "default"},
        {"id": "b", "kind": "work", "tools": "default"},
    ]
    return parse_workflow({"name": "d", "start": "fan", "nodes": nodes})


def test_parallel_choose_applies_only_the_winner(tmp_path):
    # each branch writes the same artifact in its own private copy
    def node_runner(req):
        Path(req.workspace_override, "result.txt").write_text(f"from {req.node.id}")
        return NodeRunResponse(output=f"out-{req.node.id}", session_key=None, messages=[])

    eng = WorkflowEngine(
        node_runner=node_runner, run_id_factory=lambda: "r1",
        workspace=str(tmp_path), pick_runner=lambda criteria, options, model: 1,  # pick b
    )
    res = eng.run(_writing_wf("choose", criteria="best"), "t")
    assert res.status == "completed"
    assert (tmp_path / "result.txt").read_text() == "from b"   # only the winner applied
    assert "chosen: b" in res.final_output


def test_parallel_union_applies_disjoint_branches(tmp_path):
    def node_runner(req):
        fname = "x.txt" if req.node.id == "a" else "y.txt"
        Path(req.workspace_override, fname).write_text(req.node.id)
        return NodeRunResponse(output=f"out-{req.node.id}", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1", workspace=str(tmp_path))
    res = eng.run(_writing_wf("union"), "t")
    assert res.status == "completed"
    assert (tmp_path / "x.txt").read_text() == "a"
    assert (tmp_path / "y.txt").read_text() == "b"


def test_parallel_union_conflict_aborts_and_applies_nothing(tmp_path):
    def node_runner(req):
        Path(req.workspace_override, "same.txt").write_text(req.node.id)   # both touch same path
        return NodeRunResponse(output="o", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1", workspace=str(tmp_path))
    res = eng.run(_writing_wf("union"), "t")
    assert res.status == "aborted"
    assert "conflict" in res.final_output and "same.txt" in res.final_output
    assert not (tmp_path / "same.txt").exists()   # nothing applied when there is a conflict


def test_node_exception_aborts_with_partial_trace():
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": None},
    ]})

    def node_runner(req):
        if req.node.id == "b":
            raise RuntimeError("provider exploded")
        return NodeRunResponse(output="out-a", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")
    res = eng.run(wf, "t")
    assert res.status == "aborted"                  # does not propagate the raise
    assert "provider exploded" in res.final_output
    assert [r.node_id for r in res.runs] == ["a"]   # partial trace: 'a' ran before 'b' failed


def test_parallel_choose_without_pick_runner_aborts(tmp_path):
    def node_runner(req):
        return NodeRunResponse(output="o", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1", workspace=str(tmp_path))
    res = eng.run(_writing_wf("choose", criteria="best"), "t")
    assert res.status == "aborted"


def test_parallel_respects_max_concurrency(tmp_path):
    import threading
    import time

    live = []
    lock = threading.Lock()
    peak = [0]

    def runner(req):
        with lock:
            live.append(req.node.id)
            peak[0] = max(peak[0], len(live))
        time.sleep(0.05)
        with lock:
            live.remove(req.node.id)
        return NodeRunResponse(output="x")

    nodes = [{"id": "f", "kind": "parallel", "branches": ["a", "b", "c", "d"],
              "max_concurrency": 2, "next": None}]
    nodes += [{"id": n, "kind": "work"} for n in "abcd"]
    wf = parse_workflow({"name": "w", "start": "f", "nodes": nodes})
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    assert peak[0] <= 2
