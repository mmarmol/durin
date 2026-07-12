"""Tests for the sequential flow-graph engine (graph logic, mocked node runner)."""

from pathlib import Path

import pytest

from durin.workflow.engine import (
    _SHARED_CONTEXT_MAX_MESSAGES,
    NodeRunRequest,
    NodeRunResponse,
    WorkflowEngine,
)
from durin.workflow.spec import parse_workflow


def _engine(node_outputs):
    """Engine with a scripted node runner.

    node_outputs: dict node_id -> output string.
    """
    calls = []

    def node_runner(req: NodeRunRequest) -> NodeRunResponse:
        calls.append(req)
        return NodeRunResponse(
            output=node_outputs[req.node.id],
            session_key=f"workflow:{req.run_id}:{req.node.id}:{req.iteration}",
            messages=[{"role": "assistant", "content": node_outputs[req.node.id]}],
        )

    eng = WorkflowEngine(
        node_runner=node_runner,
        run_id_factory=lambda: "r1",
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
    eng, calls = _engine({"a": "out-a", "b": "out-b"})
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
    eng, calls = _engine({"a": "out-a", "b": "out-b"})
    eng.run(wf, "do it")
    # b received a's output as upstream_output
    b_call = [c for c in calls if c.node.id == "b"][0]
    assert b_call.upstream_output == "out-a"


def test_io_descriptions_frame_the_task():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "input": {"text": True, "description": "a CSV of sales"},
        "output": {"text": True, "description": "a markdown report"},
        "nodes": [{"id": "a", "kind": "work", "next": None}],
    })
    eng, calls = _engine({"a": "out-a"})
    eng.run(wf, "do it")
    task = calls[0].task
    assert "a CSV of sales" in task
    assert "a markdown report" in task
    assert "do it" in task


def test_task_unchanged_without_io_descriptions():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "input": {"text": True},  # declared, but no description
        "nodes": [{"id": "a", "kind": "work", "next": None}],
    })
    eng, calls = _engine({"a": "out-a"})
    eng.run(wf, "do it")
    assert calls[0].task == "do it"


def test_file_input_workflow_with_files_keeps_task_unchanged(tmp_path):
    """A file-input workflow that receives input files should not frame the task.
    The node receives the task unchanged when no I/O descriptions are present."""
    src = tmp_path / "in.txt"
    src.write_text("data")
    wf = parse_workflow({
        "name": "w", "start": "a",
        "input": {"file": True},
        "nodes": [{"id": "a", "kind": "work", "tools": "default", "next": None}],
    })
    calls = []
    def runner(req):
        calls.append(req)
        return NodeRunResponse(output="x")
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "the task", input_files=[str(src)])
    assert calls and calls[0].task == "the task"


def test_per_node_max_visits_overrides_workflow_default():
    # workflow default max_visits=5, but node 'a' caps itself at 2.
    wf = parse_workflow({
        "name": "d", "start": "a", "max_visits": 5,
        "nodes": [
            {"id": "a", "kind": "work", "max_visits": 2, "on_pass": None, "on_fail": "a"},
        ],
    })
    eng, calls = _engine({"a": "FAIL keep going"})  # always FAIL → loops back to itself
    res = eng.run(wf, "t")
    assert res.status == "exhausted"
    assert res.exhausted_node == "a"
    assert len([c for c in calls if c.node.id == "a"]) == 2  # ran exactly its 2-visit budget


def test_global_ceiling_clamps_a_higher_per_node_value():
    wf = parse_workflow({
        "name": "d", "start": "a", "max_visits": 50,
        "nodes": [{"id": "a", "kind": "work", "max_visits": 50, "on_pass": None, "on_fail": "a"}],
    })
    eng, calls = _engine({"a": "FAIL"})
    eng2 = WorkflowEngine(node_runner=eng._node_runner, run_id_factory=lambda: "r1",
                          max_node_visits=3)
    res = eng2.run(wf, "t")
    assert res.status == "exhausted"
    assert len([c for c in calls if c.node.id == "a"]) == 3  # clamped to the ceiling of 3


def test_shared_vs_own_context():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "context": "shared", "next": "b"},
            {"id": "b", "kind": "work", "context": "own", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"})
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
    eng, calls = _engine({"a": "out-a", "b": "out-b"})
    eng.run(wf, "t")
    b_call = [c for c in calls if c.node.id == "b"][0]
    # b is the second shared node: it must receive a's appended message
    assert b_call.shared_context == [{"role": "assistant", "content": "out-a"}]


def test_shared_buffer_does_not_duplicate_across_three_shared_nodes():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "context": "shared", "next": "b"},
            {"id": "b", "kind": "work", "context": "shared", "next": "c"},
            {"id": "c", "kind": "work", "context": "shared", "next": None},
        ],
    })
    seen = {}

    def runner(req):
        seen[req.node.id] = list(req.shared_context)
        # Faithful to the FIXED contract: only this node's own contribution.
        return NodeRunResponse(output=f"out-{req.node.id}", messages=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": f"out-{req.node.id}"},
        ])

    WorkflowEngine(runner).run(wf, "t")
    c_ctx = seen["c"]
    assert len(c_ctx) == 4                                     # a's 2 + b's 2, no dupes
    assert [m["role"] for m in c_ctx].count("system") == 0
    assert [m["content"] for m in c_ctx].count("out-a") == 1   # each turn appears once


def test_shared_buffer_is_capped_dropping_oldest(tmp_path):
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "context": "shared", "next": "b"},
            {"id": "b", "kind": "work", "context": "shared", "next": None},
        ],
    })
    overflow = _SHARED_CONTEXT_MAX_MESSAGES + 50
    calls = []

    def node_runner(req: NodeRunRequest) -> NodeRunResponse:
        calls.append(req)
        if req.node.id == "a":
            # Emit more messages than the cap so the buffer must be trimmed.
            msgs = [{"role": "assistant", "content": f"m{i}"} for i in range(overflow)]
        else:
            msgs = []
        return NodeRunResponse(output="o", session_key=None, messages=msgs)

    WorkflowEngine(node_runner, workspace=str(tmp_path)).run(wf, "t")
    b_call = [c for c in calls if c.node.id == "b"][0]
    # b sees only the most recent N of a's messages; the oldest are dropped.
    assert len(b_call.shared_context) == _SHARED_CONTEXT_MAX_MESSAGES
    assert b_call.shared_context[0] == {"role": "assistant", "content": "m50"}
    assert b_call.shared_context[-1] == {"role": "assistant", "content": f"m{overflow - 1}"}


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



def test_subworkflow_node_runs_and_threads_output():
    from durin.workflow.spec import parse_workflow as _pw
    wf = _pw({"name": "d", "start": "sub", "nodes": [
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": "after"},
        {"id": "after", "kind": "work", "next": None},
    ]})
    calls = []

    def subworkflow_runner(name, task, root_session_key=None, work_dir=None, parent_run_id=None):
        calls.append((name, task, root_session_key, work_dir, parent_run_id))
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
    # node sessions anchor to the invoking conversation (no orphan subtrees); the
    # engine's OWN run_id is passed as the child's parent_run_id.
    assert calls == [("child", "the task", "conv:1", None, "r1")]
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


def test_subworkflow_receives_the_parent_work_dir(tmp_path):
    calls = {}
    def sub_runner(name, task, root_key, work_dir=None, parent_run_id=None):
        calls["work_dir"] = work_dir
        return "sub-out"
    wf = parse_workflow({
        "name": "w", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "tools": "default", "next": "sub"},
            {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": None},
        ],
    })
    seen = {}
    def runner(req):
        seen["a"] = req.output_dir
        return NodeRunResponse(output="x")
    WorkflowEngine(runner, subworkflow_runner=sub_runner, workspace=str(tmp_path)).run(wf, "t")
    assert calls["work_dir"] == seen["a"]          # the parent's shared folder, verbatim


def test_work_dir_override_replaces_the_run_folder(tmp_path):
    override = tmp_path / "parent-work"
    override.mkdir()
    seen = {}
    def runner(req):
        seen["a"] = req.output_dir
        return NodeRunResponse(output="x")
    wf = parse_workflow({"name": "w", "start": "a",
                         "nodes": [{"id": "a", "kind": "work", "tools": "default", "next": None}]})
    result = WorkflowEngine(runner, workspace=str(tmp_path)).run(
        wf, "t", work_dir_override=str(override))
    assert seen["a"] == str(override)
    assert result.output_dir == str(override)
    assert not (tmp_path / ".workflow").exists()   # no own run folder created


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


# ── Multi-way routing (cases) engine tests ────────────────────────────────────


def _cases_engine(node_outputs: dict) -> tuple:
    """Engine with a scripted agent runner for cases tests."""
    calls = []

    def node_runner(req: NodeRunRequest) -> NodeRunResponse:
        calls.append(req)
        return NodeRunResponse(
            output=node_outputs[req.node.id],
            session_key=f"workflow:r1:{req.node.id}:{req.iteration}",
            messages=[{"role": "assistant", "content": node_outputs[req.node.id]}],
        )

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")
    return eng, calls


def _3way_wf():
    """A 3-outcome workflow: verify routes GROUNDED→end, MISSING→plan, MISUSED→synthesize."""
    return parse_workflow({
        "name": "w", "start": "verify",
        "nodes": [
            {"id": "verify", "kind": "work",
             "cases": {"GROUNDED": None, "MISSING": "plan", "MISUSED": "synthesize"}},
            {"id": "plan", "kind": "work", "next": None},
            {"id": "synthesize", "kind": "work", "next": None},
        ],
    })


def test_cases_routes_to_grounded_end():
    """A matched label with a null target ends the run as completed."""
    wf = _3way_wf()
    eng, calls = _cases_engine({"verify": "analysis done\nGROUNDED"})
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert [r.node_id for r in res.runs] == ["verify"]


def test_cases_routes_to_missing_target():
    """A matched label with a node target routes to that node."""
    wf = _3way_wf()
    eng, calls = _cases_engine({"verify": "MISSING", "plan": "plan output"})
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert [r.node_id for r in res.runs] == ["verify", "plan"]
    assert res.final_output == "plan output"


def test_cases_routes_to_misused_target():
    """A third label routes to its distinct target."""
    wf = _3way_wf()
    eng, calls = _cases_engine({"verify": "MISUSED", "synthesize": "synth output"})
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert [r.node_id for r in res.runs] == ["verify", "synthesize"]
    assert res.final_output == "synth output"


def test_cases_default_catches_unmatched_verdict():
    """When the label doesn't match but 'default' is in cases, route to it."""
    wf = parse_workflow({
        "name": "w", "start": "classify",
        "nodes": [
            {"id": "classify", "kind": "work",
             "cases": {"DONE": None, "default": "fallback"}},
            {"id": "fallback", "kind": "work", "next": None},
        ],
    })
    eng, calls = _cases_engine({"classify": "something unrecognized", "fallback": "handled"})
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert [r.node_id for r in res.runs] == ["classify", "fallback"]


def test_cases_no_match_no_default_aborts():
    """Unmatched verdict with no default case aborts the run with a clear message."""
    wf = _3way_wf()
    eng, _ = _cases_engine({"verify": "I cannot decide"})
    res = eng.run(wf, "t")
    assert res.status == "aborted"
    assert "verify" in res.final_output
    # All expected labels should be mentioned
    for label in ["GROUNDED", "MISSING", "MISUSED"]:
        assert label in res.final_output


def test_cases_route_label_recorded_in_trace():
    """The matched case label is recorded in the NodeRun trace."""
    wf = _3way_wf()
    eng, _ = _cases_engine({"verify": "MISSING", "plan": "plan done"})
    res = eng.run(wf, "t")
    verify_run = next(r for r in res.runs if r.node_id == "verify")
    assert verify_run.route_label == "MISSING"
    assert verify_run.passed is None  # multi-way: pass/fail does not apply


def test_cases_feedback_threaded_on_loopback():
    """When a cases node routes to a non-terminal (non-null) target, its output is
    threaded into upstream_output so the target node sees why it was re-invoked."""
    seen_upstream: list = []

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        if req.node.id == "plan":
            seen_upstream.append(req.upstream_output)
            return NodeRunResponse(output="plan output")
        # verify: emit MISSING on first call, GROUNDED on second
        output = "MISSING" if len(seen_upstream) == 0 else "GROUNDED"
        return NodeRunResponse(output=output)

    wf = parse_workflow({
        "name": "w", "start": "verify", "max_visits": 5,
        "nodes": [
            {"id": "verify", "kind": "work",
             "cases": {"GROUNDED": None, "MISSING": "plan"}},
            {"id": "plan", "kind": "work", "next": "verify"},
        ],
    })
    res = WorkflowEngine(runner, run_id_factory=lambda: "r1").run(wf, "t")
    assert res.status == "completed"
    # plan must have received the verify output as reviewer feedback
    assert any("MISSING" in (u or "") for u in seen_upstream)


def test_cases_visit_cap_still_bounds_loop():
    """A cases loop-back is subject to the same visit cap as binary routing."""
    wf = parse_workflow({
        "name": "w", "start": "check", "max_visits": 2,
        "nodes": [
            {"id": "check", "kind": "work", "cases": {"RETRY": "check", "DONE": None}},
        ],
    })
    eng, calls = _cases_engine({"check": "RETRY"})  # always loops back
    res = eng.run(wf, "t")
    assert res.status == "exhausted"
    assert res.exhausted_node == "check"
    assert len(calls) == 2  # ran exactly max_visits times


def test_exhausted_node_final_output_names_the_last_producer():
    """When a routing node exhausts, final_output_node names the last non-routing
    producer before exhaustion (whose output became final_output)."""
    wf = parse_workflow({
        "name": "w", "start": "produce", "max_visits": 3,
        "nodes": [
            {"id": "produce", "kind": "work", "next": "review"},
            {"id": "review", "kind": "work", "cases": {"RETRY": "review", "DONE": None}, "max_visits": 2},
        ],
    })
    eng, _ = _cases_engine({"produce": "the draft output", "review": "RETRY"})
    res = eng.run(wf, "t")
    assert res.status == "exhausted"
    assert res.exhausted_node == "review"
    assert res.final_output == "the draft output"
    assert res.final_output_node == "produce"


def test_cases_binary_routing_regression():
    """Binary on_pass/on_fail nodes still work exactly as before (regression guard)."""
    wf = parse_workflow({
        "name": "w", "start": "prod", "max_visits": 5,
        "nodes": [
            {"id": "prod", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "on_pass": "done", "on_fail": "prod"},
            {"id": "done", "kind": "work", "next": None},
        ],
    })
    outputs = {"prod": "draft", "gate": "PASS\nok", "done": "final"}
    eng, calls = _cases_engine(outputs)
    res = eng.run(wf, "t")
    assert res.status == "completed"
    gate_run = next(r for r in res.runs if r.node_id == "gate")
    assert gate_run.passed is True
    assert gate_run.route_label is None  # binary node: no route_label


def test_cases_null_target_completes_and_final_output_is_producer():
    """A cases node routing to null (end) ends the run as 'completed'.

    final_output is the last non-routing producer's output — the cases node itself
    is the routing judge; the upstream producer ('build') is the last real producer.
    This locks the terminal-route semantics so a refactor cannot silently change them.
    """
    wf = parse_workflow({
        "name": "w", "start": "build",
        "nodes": [
            {"id": "build", "kind": "work", "next": "check"},
            {"id": "check", "kind": "work",
             "cases": {"DONE": None, "RETRY": "build"}},
        ],
    })
    eng, _ = _cases_engine({"build": "producer-output", "check": "DONE"})
    res = eng.run(wf, "t")
    assert res.status == "completed"
    # The cases node routed to null; 'build' was the last non-routing producer.
    assert res.final_output == "producer-output"


def test_frame_task_output_format_overrides_the_workflow_output_description():
    # A call-time output_format replaces the workflow's default deliverable hint for this run.
    wf = parse_workflow({
        "name": "w", "start": "a",
        "output": {"text": True, "description": "a cited prose answer"},
        "nodes": [{"id": "a", "kind": "work"}],
    })
    framed = WorkflowEngine._frame_task(wf, "the question", output_format="a 3-bullet list")
    assert "a 3-bullet list" in framed
    assert "a cited prose answer" not in framed   # the override wins for this run


def test_frame_task_without_override_uses_the_workflow_output_description():
    wf = parse_workflow({
        "name": "w", "start": "a",
        "output": {"text": True, "description": "a cited prose answer"},
        "nodes": [{"id": "a", "kind": "work"}],
    })
    framed = WorkflowEngine._frame_task(wf, "q")
    assert "a cited prose answer" in framed


def test_frame_task_output_format_works_without_a_declared_output():
    # Even a workflow with no output descriptor honors a call-time delivery instruction.
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [{"id": "a", "kind": "work"}]})
    framed = WorkflowEngine._frame_task(wf, "q", output_format="JSON with fields x,y")
    assert "JSON with fields x,y" in framed


def test_engine_passes_the_visit_budget_to_the_runner():
    wf = parse_workflow({
        "name": "d", "start": "make", "max_visits": 4,
        "nodes": [
            {"id": "make", "kind": "work", "next": "gate", "max_visits": 2},
            {"id": "gate", "kind": "work", "prompt": "ok?", "on_pass": None, "on_fail": "make"},
        ],
    })
    eng, calls = _engine({"make": "draft", "gate": "PASS"})
    eng.run(wf, "t")
    make_call = [c for c in calls if c.node.id == "make"][0]
    gate_call = [c for c in calls if c.node.id == "gate"][0]
    assert make_call.budget == 2      # per-node override
    assert gate_call.budget == 4      # workflow default


def test_node_run_records_the_effective_budget():
    wf = parse_workflow({
        "name": "d", "start": "make", "max_visits": 4,
        "nodes": [
            {"id": "make", "kind": "work", "next": None, "max_visits": 2},
        ],
    })
    eng, _calls = _engine({"make": "draft"})
    res = eng.run(wf, "t")
    assert res.runs[0].node_id == "make"
    assert res.runs[0].budget == 2


def test_gate_learns_when_a_fail_would_exhaust_the_producer():
    wf = parse_workflow({
        "name": "d", "start": "make", "max_visits": 2,
        "nodes": [
            {"id": "make", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "prompt": "ok?", "on_pass": None, "on_fail": "make"},
        ],
    })
    eng, calls = _engine({"make": "draft", "gate": "FAIL nope"})
    eng.run(wf, "t")
    gate_calls = [c for c in calls if c.node.id == "gate"]
    # 1st gate visit: producer has 1 visit of 2 → a FAIL still loops (False).
    # 2nd gate visit: producer consumed 2 of 2 → a FAIL would exhaust (True).
    assert gate_calls[0].fail_would_exhaust is False
    assert gate_calls[1].fail_would_exhaust is True


def test_sequential_nodes_share_one_working_dir(tmp_path):
    # Every sequential node — looping or hand-off — reads and writes ONE shared per-run
    # folder, so files accumulate in one place and each stage sees the prior work. (Before,
    # per-node/per-iteration folders scattered a self-loop's files and broke collaboration.)
    seen: dict = {}
    labels = iter(["MORE", "MORE", "DONE"])

    def runner(req):
        seen.setdefault(req.node.id, []).append(req.output_dir)
        out = next(labels) if req.node.id == "loop" else "ok"
        return NodeRunResponse(output=out, session_key="k", messages=[])

    eng = WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r1", workspace=str(tmp_path))
    wf = parse_workflow({
        "name": "w", "start": "loop", "max_visits": 6,
        "nodes": [
            {"id": "loop", "kind": "work", "tools": "default",
             "cases": {"MORE": "loop", "DONE": "done"}},
            {"id": "done", "kind": "work", "tools": "default", "next": None},
        ],
    })
    eng.run(wf, "go")

    all_dirs = seen["loop"] + seen["done"]
    assert len(seen["loop"]) == 3                    # the loop still looped three times
    assert len(set(all_dirs)) == 1                   # every node + iteration shares one dir
    assert all_dirs[0].endswith("/work")             # the run's shared working folder


def test_needs_input_result_names_the_asking_node():
    wf = parse_workflow({
        "name": "d", "start": "gate",
        "nodes": [{"id": "gate", "kind": "work", "prompt": "clarify?",
                   "cases": {"OK": None, "NEED_INFO": "__needs_input__"}}],
    })
    eng, _ = _engine({"gate": "what env?\nNEED_INFO"})
    result = eng.run(wf, "t")
    assert result.status == "needs_input"
    assert result.needs_input_node == "gate"
    assert result.final_output_node == "gate"
    # The routing label is transport metadata, not part of the question.
    assert result.final_output == "what env?"


def test_needs_input_keeps_output_when_label_is_the_only_line():
    wf = parse_workflow({
        "name": "d", "start": "gate",
        "nodes": [{"id": "gate", "kind": "work", "prompt": "clarify?",
                   "cases": {"OK": None, "NEED_INFO": "__needs_input__"}}],
    })
    eng, _ = _engine({"gate": "NEED_INFO"})
    result = eng.run(wf, "t")
    assert result.status == "needs_input"
    assert result.final_output == "NEED_INFO"


def test_resume_reenters_at_the_asking_node_with_carried_visits(tmp_path):
    from durin.workflow.engine import ResumeState
    wf = parse_workflow({
        "name": "d", "start": "plan",
        "nodes": [
            {"id": "plan", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "prompt": "clarify?",
             "cases": {"OK": None, "NEED_INFO": "__needs_input__"}},
        ],
    })
    eng, calls = _engine({"plan": "the plan", "gate": "OK"})
    result = eng.run(wf, "answers text", resume=ResumeState(
        run_id="fixed-run", start_at="gate",
        visits={"plan": 1, "gate": 1}, upstream="User answers:\nprod env",
    ))
    assert result.status == "completed"
    assert result.run_id == "fixed-run"
    assert [c.node.id for c in calls] == ["gate"]        # plan NOT re-run
    gate_call = calls[0]
    assert gate_call.iteration == 2                       # continues the count
    assert "prod env" in (gate_call.upstream_output or "")


def test_terminal_binary_gate_contributes_its_output():
    wf = parse_workflow({
        "name": "d", "start": "make",
        "nodes": [
            {"id": "make", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "prompt": "ok?", "on_pass": None, "on_fail": "make"},
        ],
    })
    eng, _ = _engine({"make": "the draft", "gate": "PASS\nVerified: tests green, docs updated."})
    result = eng.run(wf, "t")
    assert result.status == "completed"
    assert result.final_output == "Verified: tests green, docs updated."


def test_terminal_gate_with_bare_verdict_keeps_producer_output():
    wf = parse_workflow({
        "name": "d", "start": "make",
        "nodes": [
            {"id": "make", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "prompt": "ok?", "on_pass": None, "on_fail": "make"},
        ],
    })
    eng, _ = _engine({"make": "the draft", "gate": "PASS"})
    result = eng.run(wf, "t")
    assert result.final_output == "the draft"      # bare verdict adds nothing
    assert result.final_output_node == "make"      # residue empty — stays the producer's id


def test_terminal_cases_node_contributes_its_output():
    wf = parse_workflow({
        "name": "d", "start": "synth",
        "nodes": [
            {"id": "synth", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "prompt": "grounded?",
             "cases": {"GROUNDED": None, "MISSING": "synth"}},
        ],
    })
    eng, _ = _engine({"synth": "draft answer", "gate": "Final answer: 42.\nGROUNDED"})
    result = eng.run(wf, "t")
    assert result.final_output == "Final answer: 42."


def test_final_output_node_names_the_linear_terminal():
    wf = parse_workflow({"name": "w", "start": "a",
                         "nodes": [{"id": "a", "kind": "work", "next": None}]})
    eng, _ = _engine({"a": "out"})
    assert eng.run(wf, "t").final_output_node == "a"


def test_final_output_node_names_a_contributing_terminal_gate():
    wf = parse_workflow({"name": "w", "start": "make",
                         "nodes": [
                             {"id": "make", "kind": "work", "next": "gate"},
                             {"id": "gate", "kind": "work", "prompt": "ok?",
                              "on_pass": None, "on_fail": "make"}]})
    eng, _ = _engine({"make": "draft", "gate": "PASS\nVerified."})
    r = eng.run(wf, "t")
    assert r.final_output == "Verified." and r.final_output_node == "gate"


def test_cancelled_result_names_the_last_producer_node():
    """When cancel_check returns True between nodes, the run ends with status='cancelled'
    and final_output_node names the last node that ran (whose output became final_output)."""
    wf = parse_workflow({
        "name": "w", "start": "first",
        "nodes": [
            {"id": "first", "kind": "work", "next": "second"},
            {"id": "second", "kind": "work", "next": None},
        ],
    })
    # cancel_check returns True after the first node runs
    cancel_on_second = [False]
    def cancel_check():
        if cancel_on_second[0]:
            return True
        cancel_on_second[0] = True
        return False

    eng = WorkflowEngine(
        node_runner=lambda req: NodeRunResponse(
            output=f"output-{req.node.id}", session_key=None, messages=[]
        ),
        run_id_factory=lambda: "r1",
        cancel_check=cancel_check,
    )
    res = eng.run(wf, "t")
    assert res.status == "cancelled"
    assert res.final_output == "output-first"
    assert res.final_output_node == "first"
