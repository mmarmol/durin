"""Tests for the workflow self-improvement dream pass (model injected, no live LLM)."""

import json
from types import SimpleNamespace

from durin.workflow import run_log, workflow_recommendations as wr
from durin.workflow.loader import workflows_dir
from durin.workflow.result import NodeRun, WorkflowResult
from durin.workflow.workflow_improve_dream import run_workflow_improve_pass

_WF = {
    "name": "wf", "start": "a", "improvement_mode": "manual",
    "nodes": [
        {"id": "a", "kind": "work", "prompt": "do it", "next": "g"},
        {"id": "g", "kind": "decision", "criteria": "is it good?", "on_pass": None, "on_fail": "a"},
    ],
}


def _write_wf(tmp_path, data=_WF, name="wf"):
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def _looping_run(run_id):
    # node 'a' ran twice (loop-back); gate 'g' failed once then passed
    return WorkflowResult(
        status="completed", final_output="x", run_id=run_id,
        runs=[
            NodeRun(node_id="a", iteration=1, output="o"),
            NodeRun(node_id="g", iteration=1, output="", passed=False),
            NodeRun(node_id="a", iteration=2, output="o"),
            NodeRun(node_id="g", iteration=2, output="", passed=True),
        ],
    )


def _seed_runs(tmp_path, name="wf", n=2):
    for i in range(n):
        run_log.write_run(tmp_path, name, _looping_run(f"r{i}"), ts=float(i + 1))


def _fake_invoke(payload):
    def invoke(prompt, *, model=None):
        return SimpleNamespace(content=json.dumps(payload) if isinstance(payload, dict) else payload)
    return invoke


def test_pass_records_a_recommendation_for_a_looping_node(tmp_path):
    _write_wf(tmp_path)
    _seed_runs(tmp_path, n=2)   # recurs -> candidate
    invoke = _fake_invoke({
        "target_id": "a", "field": "prompt", "current": "do it",
        "proposed": "do it carefully, validating each step", "reason": "node a keeps looping",
    })
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary == {"workflows": 1, "proposals": 1}
    recs = wr.open_recommendations(tmp_path, "wf")
    assert len(recs) == 1
    assert recs[0]["target_id"] == "a" and recs[0]["field"] == "prompt"
    assert "validating" in recs[0]["proposed"]
    # cursor advanced -> a second pass with no new runs proposes nothing
    again = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert again == {"workflows": 0, "proposals": 0}


def test_off_mode_workflow_is_ignored(tmp_path):
    data = dict(_WF, improvement_mode="off")
    _write_wf(tmp_path, data)
    _seed_runs(tmp_path, n=3)
    invoke = _fake_invoke({"target_id": "a", "field": "prompt", "proposed": "x", "reason": "y"})
    assert run_workflow_improve_pass(tmp_path, llm_invoke=invoke) == {"workflows": 0, "proposals": 0}
    assert wr.open_recommendations(tmp_path, "wf") == []


def test_structural_proposal_is_rejected(tmp_path):
    _write_wf(tmp_path)
    _seed_runs(tmp_path, n=2)
    invoke = _fake_invoke({"action": "add_node", "id": "z"})   # out of scope
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary == {"workflows": 1, "proposals": 0}          # processed, but nothing proposed
    assert wr.open_recommendations(tmp_path, "wf") == []


def test_field_node_type_mismatch_is_rejected(tmp_path):
    _write_wf(tmp_path)
    _seed_runs(tmp_path, n=2)
    # 'criteria' on a work node, or 'prompt' on a decision node -> rejected
    invoke = _fake_invoke({"target_id": "a", "field": "criteria", "proposed": "x", "reason": "y"})
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["proposals"] == 0


def test_no_op_proposal_is_rejected(tmp_path):
    _write_wf(tmp_path)
    _seed_runs(tmp_path, n=2)
    # the model proposes text identical to the node's current prompt — a no-op
    invoke = _fake_invoke({"target_id": "a", "field": "prompt", "proposed": "do it", "reason": "x"})
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["proposals"] == 0          # nothing changes -> not queued
    assert wr.open_recommendations(tmp_path, "wf") == []


def test_one_off_trouble_below_floor_proposes_nothing(tmp_path):
    _write_wf(tmp_path)
    _seed_runs(tmp_path, n=1)   # single run -> below recurrence floor
    invoke = _fake_invoke({"target_id": "a", "field": "prompt", "proposed": "x", "reason": "y"})
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary == {"workflows": 0, "proposals": 0}
