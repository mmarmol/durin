"""Detached nodes: side effects that run beside the walk without blocking it.

Live motivation (mxHero box, 2026-07-22): persist-memory had to be EXTRACTED to
a separate workflow because every node sits on the critical path and feeds the
edge — entity persistence delayed the ticket answer by a minute for a result
nobody downstream reads. A ``detached: true`` node is launched and the walk
continues immediately; the edge text passes through unchanged; its output never
becomes final_output; every terminal path joins it so the manifest is complete;
and its failure records ``node_failed`` without sinking the run.
"""

import threading

import pytest

from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.script_runner import ScriptNodeRunner
from durin.workflow.spec import WorkflowError, parse_workflow


def _wf(nodes):
    return parse_workflow({"name": "d", "start": nodes[0]["id"], "nodes": nodes})


# ── spec ──

def test_detached_parses_on_work_and_script():
    wf = _wf([
        {"id": "a", "kind": "work", "next": "side"},
        {"id": "side", "kind": "work", "detached": True, "next": "b"},
        {"id": "s", "kind": "script", "command": "true", "detached": True, "next": None},
        {"id": "b", "kind": "work", "next": "s"},
    ])
    assert wf.nodes["side"].detached is True
    assert wf.nodes["s"].detached is True


def test_detached_rejects_routing():
    with pytest.raises(WorkflowError, match="detached"):
        _wf([
            {"id": "a", "kind": "work", "detached": True,
             "on_pass": None, "on_fail": "b"},
            {"id": "b", "kind": "work", "next": None},
        ])
    with pytest.raises(WorkflowError, match="detached"):
        _wf([{"id": "a", "kind": "work", "detached": True,
              "cases": {"X": None}}])


def test_detached_rejects_shared_context():
    with pytest.raises(WorkflowError, match="detached"):
        _wf([{"id": "a", "kind": "work", "detached": True, "context": "shared",
              "next": None}])


def test_detached_cannot_be_a_routing_target():
    with pytest.raises(WorkflowError, match="detached"):
        _wf([
            {"id": "make", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "on_pass": None, "on_fail": "side"},
            {"id": "side", "kind": "work", "detached": True, "next": None},
        ])


def test_detached_cannot_be_a_parallel_branch():
    with pytest.raises(WorkflowError, match="detached"):
        _wf([
            {"id": "fan", "kind": "parallel", "branches": ["side", "b"], "next": None},
            {"id": "side", "kind": "work", "detached": True, "next": None},
            {"id": "b", "kind": "work"},
        ])


# ── engine ──

def _chain():
    return _wf([
        {"id": "a", "kind": "work", "next": "side"},
        {"id": "side", "kind": "work", "detached": True, "next": "b"},
        {"id": "b", "kind": "work", "next": None},
    ])


def test_walk_continues_while_detached_runs_and_edge_passes_through():
    b_ran = threading.Event()
    seen = {}

    def node_runner(req):
        if req.node.id == "side":
            # The detached node finishes only AFTER b has run: if the walk
            # blocked on it, this would deadlock (b never runs → never set).
            assert b_ran.wait(timeout=5), "walk blocked on the detached node"
            seen["side_upstream"] = req.upstream_output
            return NodeRunResponse(output="side-out")
        if req.node.id == "b":
            seen["b_upstream"] = req.upstream_output
            b_ran.set()
        return NodeRunResponse(output=f"{req.node.id}-out")

    res = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1").run(_chain(), "t")
    assert res.status == "completed"
    assert seen["b_upstream"] == "a-out"          # edge passed through untouched
    assert seen["side_upstream"] == "a-out"       # the detached node saw the same input
    assert res.final_output == "b-out"            # never the detached output
    side = next(r for r in res.runs if r.node_id == "side")
    assert side.output == "side-out"              # joined + recorded before returning
    assert side.duration_s is not None


def test_detached_failure_records_node_failed_without_sinking_the_run():
    def node_runner(req):
        if req.node.id == "side":
            raise RuntimeError("persist blew up")
        return NodeRunResponse(output=f"{req.node.id}-out")

    res = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1").run(_chain(), "t")
    assert res.status == "completed"              # the side effect must not sink the run
    side = next(r for r in res.runs if r.node_id == "side")
    assert side.status == "node_failed"
    assert side.error and "persist blew up" in side.error


def test_terminal_detached_keeps_the_previous_output_as_final():
    wf = _wf([
        {"id": "a", "kind": "work", "next": "side"},
        {"id": "side", "kind": "work", "detached": True, "next": None},
    ])

    def node_runner(req):
        return NodeRunResponse(output=f"{req.node.id}-out")

    res = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1").run(wf, "t")
    assert res.status == "completed"
    assert res.final_output == "a-out"
    assert any(r.node_id == "side" for r in res.runs)


def test_detached_script_records_exit_code(tmp_path):
    d = tmp_path / "workflows" / "scripts"
    d.mkdir(parents=True)
    (d / "side.py").write_text("import sys\nsys.exit(2)\n")
    wf = _wf([
        {"id": "a", "kind": "work", "next": "side"},
        {"id": "side", "kind": "script", "script": "side.py", "detached": True, "next": "b"},
        {"id": "b", "kind": "work", "next": None},
    ])

    def node_runner(req):
        return NodeRunResponse(output=f"{req.node.id}-out")

    eng = WorkflowEngine(node_runner=node_runner, script_runner=ScriptNodeRunner(tmp_path),
                         run_id_factory=lambda: "r1", workspace=str(tmp_path))
    res = eng.run(wf, "t")
    assert res.status == "completed"
    side = next(r for r in res.runs if r.node_id == "side")
    assert side.status == "node_failed"
    assert side.exit_code == 2


def test_needs_input_return_still_joins_the_detached_node():
    wf = _wf([
        {"id": "a", "kind": "work", "next": "side"},
        {"id": "side", "kind": "work", "detached": True, "next": "gate"},
        {"id": "gate", "kind": "work",
         "cases": {"OK": None, "ASK": "__needs_input__"}},
    ])

    def node_runner(req):
        if req.node.id == "gate":
            return NodeRunResponse(output="ASK\nwhich org?")
        return NodeRunResponse(output=f"{req.node.id}-out")

    res = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1").run(wf, "t")
    assert res.status == "needs_input"
    assert any(r.node_id == "side" for r in res.runs)   # joined even on the pause path
