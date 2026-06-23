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


def test_depth_cap_stops_runaway_recursion(tmp_path):
    # 'loop' references itself via a subworkflow node → would recurse forever
    # without the depth cap. The cap returns an error string at the limit.
    _write(tmp_path, "loop", {"name": "loop", "start": "s",
                              "nodes": [{"id": "s", "kind": "subworkflow", "workflow": "loop", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("x"), judge_runner=None, max_depth=3)
    out = runner("loop", "t")
    assert "depth" in out.lower()
