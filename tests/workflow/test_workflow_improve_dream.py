"""Tests for the workflow self-improvement dream pass (model injected, no live LLM)."""

import json
from types import SimpleNamespace

from durin.workflow import run_log, workflow_recommendations as wr
from durin.workflow.loader import workflows_dir
from durin.workflow.result import NodeRun, WorkflowResult
from durin.workflow.workflow_improve_dream import run_workflow_improve_pass

# Node 'g' is a routing WorkNode with on_pass/on_fail (its prompt is the verdict criterion).
_WF = {
    "name": "wf", "start": "a", "improvement_mode": "manual",
    "nodes": [
        {"id": "a", "kind": "work", "prompt": "do it", "next": "g"},
        {"id": "g", "kind": "work", "prompt": "is it good?", "on_pass": None, "on_fail": "a"},
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
    assert summary == {"workflows": 1, "proposals": 1, "applied": 0, "structural": 0, "reverted": 0}
    recs = wr.open_recommendations(tmp_path, "wf")
    assert len(recs) == 1
    assert recs[0]["target_id"] == "a" and recs[0]["field"] == "prompt"
    assert "validating" in recs[0]["proposed"]
    # cursor advanced -> a second pass with no new runs proposes nothing
    again = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert again == {"workflows": 0, "proposals": 0, "applied": 0, "structural": 0, "reverted": 0}


def test_auto_mode_applies_the_edit_and_holds_it_pending_validation(tmp_path):
    data = dict(_WF, improvement_mode="auto")
    _write_wf(tmp_path, data)
    _seed_runs(tmp_path, n=3)
    invoke = _fake_invoke({
        "target_id": "a", "field": "prompt", "current": "do it",
        "proposed": "do it carefully", "reason": "node a keeps looping",
    })
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["applied"] == 1 and summary["proposals"] == 1
    from durin.workflow.loader import load_workflow
    from durin.workflow.version_store import history_for_dream
    assert load_workflow(tmp_path, "wf").nodes["a"].prompt == "do it carefully"
    assert history_for_dream(tmp_path, "wf")
    assert wr.open_recommendations(tmp_path, "wf") == []      # applied = terminal
    from durin.workflow.workflow_improve_dream import _read_pending
    pending = _read_pending(tmp_path, "wf")
    assert pending and pending["target_id"] == "a" and pending["previous"] == "do it"


def test_structural_proposal_lands_in_the_bandeja_annotated(tmp_path):
    _write_wf(tmp_path)
    _seed_runs(tmp_path, n=2)
    invoke = _fake_invoke({"target_id": "z", "field": "prompt",
                           "proposed": "add a verification node after g",
                           "reason": "the loop needs a checker"})   # unknown target -> out of scope
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["structural"] == 1 and summary["proposals"] == 0
    recs = wr.open_recommendations(tmp_path, "wf")
    assert len(recs) == 1
    assert recs[0]["kind"] == "structural"
    assert "not an editable node" in recs[0]["why_rejected"]
    assert "loop-backs" in recs[0]["diagnostic"]          # the evidence travels with it
    out = wr.apply_recommendation(tmp_path, "wf", recs[0]["id"], actor="dream")
    assert out["ok"] is False and "structural" in out["error"]


def test_unparseable_structural_shape_is_skipped_quietly(tmp_path):
    _write_wf(tmp_path)
    _seed_runs(tmp_path, n=2)
    invoke = _fake_invoke({"action": "add_node", "id": "z"})   # no proposed text at all
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["structural"] == 0 and summary["proposals"] == 0
    assert wr.open_recommendations(tmp_path, "wf") == []


def test_field_node_type_mismatch_is_rejected(tmp_path):
    _write_wf(tmp_path)
    _seed_runs(tmp_path, n=2)
    # 'criteria' is no longer a valid field for any node (all nodes use 'prompt') -> rejected
    invoke = _fake_invoke({"target_id": "a", "field": "criteria", "proposed": "x", "reason": "y"})
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["proposals"] == 0


def test_routing_node_prompt_is_editable(tmp_path):
    _write_wf(tmp_path)
    _seed_runs(tmp_path, n=2)
    # a routing WorkNode's prompt is the editable field
    invoke = _fake_invoke({
        "target_id": "g", "field": "prompt", "current": "is it good?",
        "proposed": "does the output fully address the task?", "reason": "more precise",
    })
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary == {"workflows": 1, "proposals": 1, "applied": 0, "structural": 0, "reverted": 0}
    recs = wr.open_recommendations(tmp_path, "wf")
    assert len(recs) == 1
    assert recs[0]["target_id"] == "g" and recs[0]["field"] == "prompt"


def test_prompt_proposal_on_a_script_node_is_structural_not_skipped(tmp_path):
    # A script node has no 'prompt' field: a field:'prompt' proposal aimed at it
    # is out of the prompt-only scope and must escalate, not be silently skipped.
    data = {
        "name": "wf", "start": "a", "improvement_mode": "manual",
        "nodes": [
            {"id": "a", "kind": "work", "prompt": "do it", "next": "g"},
            {"id": "g", "kind": "work", "prompt": "is it good?", "on_pass": None, "on_fail": "a"},
            {"id": "gate", "kind": "script", "command": "true"},
        ],
    }
    _write_wf(tmp_path, data)
    _seed_runs(tmp_path, n=2)
    invoke = _fake_invoke({
        "target_id": "gate", "field": "prompt",
        "proposed": "be stricter", "reason": "gate keeps passing bad output",
    })
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["structural"] == 1 and summary["proposals"] == 0 and summary["applied"] == 0
    recs = wr.open_recommendations(tmp_path, "wf")
    assert len(recs) == 1
    assert recs[0]["kind"] == "structural"
    assert "outside the prompt-only scope" in recs[0]["why_rejected"]


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
    assert summary == {"workflows": 0, "proposals": 0, "applied": 0, "structural": 0, "reverted": 0}


# -- auto-revert: an applied edit must prove itself on the next runs ------------

def _clean_run(run_id):
    return WorkflowResult(
        status="completed", final_output="x", run_id=run_id,
        runs=[NodeRun(node_id="a", iteration=1, output="o"),
              NodeRun(node_id="g", iteration=1, output="", passed=True)],
    )


def _auto_apply(tmp_path):
    """Seed an auto workflow, apply one edit (baseline 2/3 troubled runs)."""
    _write_wf(tmp_path, dict(_WF, improvement_mode="auto"))
    run_log.write_run(tmp_path, "wf", _looping_run("r0"), ts=1.0)
    run_log.write_run(tmp_path, "wf", _looping_run("r1"), ts=2.0)
    run_log.write_run(tmp_path, "wf", _clean_run("r2"), ts=3.0)
    invoke = _fake_invoke({"target_id": "a", "field": "prompt", "current": "do it",
                           "proposed": "do it carefully", "reason": "loops"})
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["applied"] == 1
    return invoke


def test_worsened_diagnostic_auto_reverts_the_edit(tmp_path):
    invoke = _auto_apply(tmp_path)
    # Post-edit runs: node 'a' loops in EVERY run (1.0 > baseline 0.67) -> worse.
    run_log.write_run(tmp_path, "wf", _looping_run("r3"), ts=4.0)
    run_log.write_run(tmp_path, "wf", _looping_run("r4"), ts=5.0)
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["reverted"] == 1
    from durin.workflow.loader import load_workflow
    from durin.workflow.workflow_improve_dream import _read_pending
    assert load_workflow(tmp_path, "wf").nodes["a"].prompt == "do it"   # restored
    assert _read_pending(tmp_path, "wf") is None                        # marker consumed
    from durin.workflow.workflow_recommendations import _path, _read
    rec = _read(_path(tmp_path, "wf"))[0]
    assert rec["status"] == "reverted"
    assert "worsened" in rec["revert_note"]
    # The revert is in the version history the NEXT pass reads -> not re-proposed.
    from durin.workflow.version_store import history_for_dream
    assert any("auto-revert" in h.get("reason", "") for h in history_for_dream(tmp_path, "wf"))


def test_improved_diagnostic_validates_and_clears_the_marker(tmp_path):
    invoke = _auto_apply(tmp_path)
    run_log.write_run(tmp_path, "wf", _clean_run("r3"), ts=4.0)
    run_log.write_run(tmp_path, "wf", _clean_run("r4"), ts=5.0)
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["reverted"] == 0
    from durin.workflow.loader import load_workflow
    from durin.workflow.workflow_improve_dream import _read_pending
    assert load_workflow(tmp_path, "wf").nodes["a"].prompt == "do it carefully"  # kept
    assert _read_pending(tmp_path, "wf") is None                                 # validated


# -- script repair lane: command / script_file proposals, precheck, revert -----

def _script_failed_run(run_id, node_id="s", error="boom: exit 1"):
    return WorkflowResult(
        status="aborted", final_output=None, run_id=run_id, failed_node=node_id,
        runs=[NodeRun(node_id=node_id, iteration=1, output="", status="node_failed",
                      error=error, exit_code=1)],
    )


def _script_clean_run(run_id, node_id="s"):
    return WorkflowResult(
        status="completed", final_output="ok", run_id=run_id,
        runs=[NodeRun(node_id=node_id, iteration=1, output="ok", status="ok", exit_code=0)],
    )


def _linear_script_wf(name="s_wf", improvement_mode="auto", command="false"):
    return {
        "name": name, "start": "s", "improvement_mode": improvement_mode,
        "nodes": [{"id": "s", "kind": "script", "command": command, "next": None}],
    }


def _routing_script_wf(name="gate_wf", improvement_mode="auto", command="false"):
    return {
        "name": name, "start": "s", "improvement_mode": improvement_mode,
        "nodes": [{"id": "s", "kind": "script", "command": command, "on_fail": "s"}],
    }


def _script_file_wf(name="file_wf", improvement_mode="auto", script="check.sh"):
    return {
        "name": name, "start": "s", "improvement_mode": improvement_mode,
        "nodes": [{"id": "s", "kind": "script", "script": script, "next": None}],
    }


def test_linear_script_command_fix_auto_applies_and_reverts_on_worsened(tmp_path):
    data = _linear_script_wf()
    _write_wf(tmp_path, data, name="s_wf")
    # baseline: 2 of 3 runs fail -> 0.67 trouble/run (matches the recurrence floor)
    run_log.write_run(tmp_path, "s_wf", _script_failed_run("r0"), ts=1.0)
    run_log.write_run(tmp_path, "s_wf", _script_failed_run("r1"), ts=2.0)
    run_log.write_run(tmp_path, "s_wf", _script_clean_run("r2"), ts=3.0)
    invoke = _fake_invoke({"target_id": "s", "field": "command",
                           "proposed": "echo fixed", "reason": "node s keeps crashing"})
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["applied"] == 1 and summary["proposals"] == 1
    from durin.workflow.loader import load_workflow
    assert load_workflow(tmp_path, "s_wf").nodes["s"].command == "echo fixed"   # definition on disk changed
    recs = wr.open_recommendations(tmp_path, "s_wf")
    assert recs == []                                                          # applied = terminal
    from durin.workflow.workflow_improve_dream import _read_pending
    pending = _read_pending(tmp_path, "s_wf")
    assert pending and pending["kind"] == "command" and pending["target_id"] == "s"
    assert pending["previous"] == "false"

    # Post-edit runs: node 's' fails in EVERY run (1.0 > baseline 0.67) -> worse -> revert.
    run_log.write_run(tmp_path, "s_wf", _script_failed_run("r3"), ts=4.0)
    run_log.write_run(tmp_path, "s_wf", _script_failed_run("r4"), ts=5.0)
    summary2 = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary2["reverted"] == 1
    assert load_workflow(tmp_path, "s_wf").nodes["s"].command == "false"        # restored
    assert _read_pending(tmp_path, "s_wf") is None
    from durin.workflow.workflow_recommendations import _path, _read
    rec = _read(_path(tmp_path, "s_wf"))[0]
    assert rec["status"] == "reverted"


def test_routing_gate_command_proposal_lands_manual_only_not_applied(tmp_path):
    data = _routing_script_wf()
    _write_wf(tmp_path, data, name="gate_wf")
    run_log.write_run(tmp_path, "gate_wf", _script_failed_run("r0"), ts=1.0)
    run_log.write_run(tmp_path, "gate_wf", _script_failed_run("r1"), ts=2.0)
    invoke = _fake_invoke({"target_id": "s", "field": "command",
                           "proposed": "echo fixed", "reason": "gate keeps failing"})
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["proposals"] == 1 and summary["applied"] == 0
    recs = wr.open_recommendations(tmp_path, "gate_wf")
    assert len(recs) == 1
    assert recs[0]["target_id"] == "s" and recs[0]["field"] == "command"
    assert recs[0].get("manual_only") is True
    from durin.workflow.loader import load_workflow
    assert load_workflow(tmp_path, "gate_wf").nodes["s"].command == "false"     # unchanged
    from durin.workflow.workflow_improve_dream import _read_pending
    assert _read_pending(tmp_path, "gate_wf") is None


def test_script_file_fix_auto_applies_and_revert_restores_exact_bytes(tmp_path):
    data = _script_file_wf()
    _write_wf(tmp_path, data, name="file_wf")
    scripts_dir = workflows_dir(tmp_path) / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    original = "#!/bin/bash\nexit 1\n"
    (scripts_dir / "check.sh").write_text(original, encoding="utf-8")

    run_log.write_run(tmp_path, "file_wf", _script_failed_run("r0"), ts=1.0)
    run_log.write_run(tmp_path, "file_wf", _script_failed_run("r1"), ts=2.0)
    run_log.write_run(tmp_path, "file_wf", _script_clean_run("r2"), ts=3.0)
    invoke = _fake_invoke({"field": "script_file", "script": "check.sh",
                           "proposed": "#!/bin/bash\necho fixed\n", "reason": "script keeps crashing"})
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["applied"] == 1 and summary["proposals"] == 1
    assert (scripts_dir / "check.sh").read_text(encoding="utf-8") == "#!/bin/bash\necho fixed\n"
    from durin.workflow.workflow_improve_dream import _read_pending
    pending = _read_pending(tmp_path, "file_wf")
    assert pending and pending["kind"] == "script_file" and pending["script"] == "check.sh"
    assert pending["previous_content"] == original

    run_log.write_run(tmp_path, "file_wf", _script_failed_run("r3"), ts=4.0)
    run_log.write_run(tmp_path, "file_wf", _script_failed_run("r4"), ts=5.0)
    summary2 = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary2["reverted"] == 1
    assert (scripts_dir / "check.sh").read_text(encoding="utf-8") == original   # exact bytes restored
    assert _read_pending(tmp_path, "file_wf") is None


def test_script_node_with_no_failure_evidence_is_structural(tmp_path):
    data = {
        "name": "wf", "start": "a", "improvement_mode": "manual",
        "nodes": [
            {"id": "a", "kind": "work", "prompt": "do it", "next": "g"},
            {"id": "g", "kind": "work", "prompt": "is it good?", "on_pass": None, "on_fail": "a"},
            {"id": "s", "kind": "script", "command": "true", "next": None},
        ],
    }
    _write_wf(tmp_path, data)
    _seed_runs(tmp_path, n=2)   # 'a'/'g' loop -> candidates non-empty; 's' has no evidence at all
    invoke = _fake_invoke({"target_id": "s", "field": "command",
                           "proposed": "echo fixed", "reason": "let's fix it anyway"})
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["structural"] == 1 and summary["proposals"] == 0 and summary["applied"] == 0
    recs = wr.open_recommendations(tmp_path, "wf")
    assert len(recs) == 1
    assert recs[0]["kind"] == "structural"
    assert "no recurring script-failure evidence" in recs[0]["why_rejected"]


def test_precheck_failing_command_proposal_is_structural_with_syntax_detail(tmp_path):
    data = _linear_script_wf()
    _write_wf(tmp_path, data, name="s_wf")
    run_log.write_run(tmp_path, "s_wf", _script_failed_run("r0"), ts=1.0)
    run_log.write_run(tmp_path, "s_wf", _script_failed_run("r1"), ts=2.0)
    invoke = _fake_invoke({"target_id": "s", "field": "command",
                           "proposed": "if [ 1 -eq 1 ]; then echo hi", "reason": "fix it"})
    summary = run_workflow_improve_pass(tmp_path, llm_invoke=invoke)
    assert summary["structural"] == 1 and summary["proposals"] == 0 and summary["applied"] == 0
    recs = wr.open_recommendations(tmp_path, "s_wf")
    assert len(recs) == 1
    assert recs[0]["kind"] == "structural"
    detail = recs[0]["why_rejected"].lower()
    assert "syntax" in detail or "unexpected" in detail
    from durin.workflow.loader import load_workflow
    assert load_workflow(tmp_path, "s_wf").nodes["s"].command == "false"        # untouched
