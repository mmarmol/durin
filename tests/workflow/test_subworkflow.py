"""Tests for the default subworkflow runner (load + nested engine + depth cap)."""

import json

from durin.workflow.engine import NodeRunResponse
from durin.workflow.loader import workflows_dir
from durin.workflow.subworkflow import SubworkflowRunner


def _write(workspace, name, data):
    d = workflows_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def _node_runner(output):
    def nr(req):
        return NodeRunResponse(output=output, session_key=None, messages=[])
    return nr


def test_runs_named_workflow_and_returns_final_output(tmp_path):
    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("child-result"), judge_runner=None)
    out = runner("child", "do the child")
    assert out == "child-result"


def test_missing_subworkflow_returns_error_not_raise(tmp_path):
    runner = SubworkflowRunner(tmp_path, _node_runner("x"), judge_runner=None)
    out = runner("ghost", "t")
    assert "ghost" in out or "Error" in out


def test_depth_cap_stops_deep_non_cyclic_nesting(tmp_path):
    # A calls B, B calls C, C calls D — no cycle, but deep enough to hit the depth cap.
    # The cap returns an error string at the limit.
    _write(tmp_path, "A", {"name": "A", "start": "s",
                           "nodes": [{"id": "s", "kind": "subworkflow", "workflow": "B", "next": None}]})
    _write(tmp_path, "B", {"name": "B", "start": "s",
                           "nodes": [{"id": "s", "kind": "subworkflow", "workflow": "C", "next": None}]})
    _write(tmp_path, "C", {"name": "C", "start": "s",
                           "nodes": [{"id": "s", "kind": "subworkflow", "workflow": "D", "next": None}]})
    _write(tmp_path, "D", {"name": "D", "start": "s",
                           "nodes": [{"id": "s", "kind": "work", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("x"), judge_runner=None, max_depth=2)
    out = runner("A", "t")
    assert "depth" in out.lower()


def test_subworkflow_cycle_is_detected(tmp_path):
    # workflow A has a subworkflow node calling A again; the call-stack guard
    # must stop on reentry with a clear cycle error, not a generic depth error.
    _write(tmp_path, "A", {"name": "A", "start": "call",
                           "nodes": [{"id": "call", "kind": "subworkflow", "workflow": "A", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("x"), judge_runner=None)
    out = runner("A", task="go")
    assert "cycle detected" in out
    assert "A -> A" in out


def test_parent_run_id_forwarded_to_nested_manifest(tmp_path):
    from durin.workflow import run_log

    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("child-result"), judge_runner=None)
    runner("child", "do the child", parent_run_id="parent1")

    wf_dir = tmp_path / "workflows-runs" / "child"
    manifests = list(wf_dir.glob("*.json"))
    assert len(manifests) == 1
    rec = run_log.read_manifest(tmp_path, "child", manifests[0].stem)
    assert rec["parent_run_id"] == "parent1"


def test_parent_run_id_defaults_to_none(tmp_path):
    from durin.workflow import run_log

    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("child-result"), judge_runner=None)
    runner("child", "do the child")

    wf_dir = tmp_path / "workflows-runs" / "child"
    manifests = list(wf_dir.glob("*.json"))
    rec = run_log.read_manifest(tmp_path, "child", manifests[0].stem)
    assert rec["parent_run_id"] is None


def test_subworkflow_runs_script_nodes(tmp_path):
    # "echo -n ABC" (not something reading stdin) so the assertion exercises only
    # wiring — a script node whose own command produces output deterministically,
    # with no agent node runner involved at all (the sentinel below would raise).
    from durin.workflow.script_runner import ScriptNodeRunner

    _write(tmp_path, "child", {
        "name": "child", "start": "s",
        "nodes": [{"id": "s", "kind": "script", "command": "echo -n ABC", "next": None}],
    })
    runner = SubworkflowRunner(
        tmp_path,
        node_runner=lambda req: (_ for _ in ()).throw(AssertionError("no agent node here")),
        judge_runner=None,
        script_runner=ScriptNodeRunner(tmp_path),
    )
    assert runner("child", "abc").strip() == "ABC"


def test_nested_nodes_work_in_the_parent_folder(tmp_path):
    _write(tmp_path, "child", {
        "name": "child", "start": "c",
        "nodes": [{"id": "c", "kind": "work", "tools": "default", "next": None}],
    })
    seen = {}
    def node_runner(req):
        seen["c"] = req.output_dir
        return NodeRunResponse(output="child-out")
    parent_work = tmp_path / "parent-work"
    parent_work.mkdir()
    out = SubworkflowRunner(tmp_path, node_runner)("child", "task", None, work_dir=str(parent_work))
    assert out == "child-out"
    assert seen["c"] == str(parent_work)
