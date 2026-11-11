"""A node's trace row records what ran and who produced it on every path.

The linear walk copies a runner's evidence into the row: an agent node's resolved
model and provider plus the hash of its definition, a script node's command and
captured streams, and whether its output was cut. A node that runs as a parallel
branch, a fan-out worker or a detached side effect is the same node with the same
runner, so its row must carry the same evidence — otherwise a reader of the run
cannot tell which model wrote a branch's output or what a branch script ran.
"""

import json
from pathlib import Path

from durin.workflow import provenance, run_log
from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.script_runner import ScriptNodeRunner
from durin.workflow.spec import parse_workflow


def _agent(req):
    """A stand-in agent runner that reports its producer like the real one does."""
    return NodeRunResponse(output=f"{req.node.id}-out",
                           session_key=f"workflow:r1:{req.node.id}:1",
                           model="model-x", provider="provider-y")


def _engine(tmp_path, node_runner=_agent, **kw):
    return WorkflowEngine(node_runner=node_runner, script_runner=ScriptNodeRunner(tmp_path, **kw.pop("script", {})),
                          run_id_factory=lambda: "r1", workspace=str(tmp_path), **kw)


def _mixed(reconcile="read", **extra):
    return parse_workflow({"name": "w", "start": "fan", "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["think", "fetch"],
         "reconcile": reconcile, "next": "join", **extra},
        {"id": "think", "kind": "work"},
        {"id": "fetch", "kind": "script", "command": "echo fetched; echo note >&2"},
        {"id": "join", "kind": "work", "next": None},
    ]})


def _row(res, node_id):
    return next(r for r in res.runs if r.node_id == node_id)


# ── static branches sharing the working folder ──

def test_agent_branch_records_its_model_provider_and_definition_hash(tmp_path):
    wf = _mixed()
    res = _engine(tmp_path).run(wf, "t")
    assert res.status == "completed"
    think = _row(res, "think")
    assert think.branch_id == "think"
    assert (think.model, think.provider) == ("model-x", "provider-y")
    assert think.node_hash == provenance.reuse_hash(wf.nodes["think"].raw)


def test_script_branch_records_its_command_and_streams(tmp_path):
    res = _engine(tmp_path).run(_mixed(), "t")
    fetch = _row(res, "fetch")
    assert fetch.exit_code == 0
    assert "echo fetched" in fetch.command
    assert fetch.stdout.strip() == "fetched"
    assert fetch.stderr.strip() == "note"
    assert (fetch.model, fetch.provider) == (None, None)   # a script has no model


def test_failed_script_branch_keeps_what_it_ran_and_printed(tmp_path):
    wf = parse_workflow({"name": "w", "start": "fan", "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["think", "fetch"],
         "reconcile": "read", "next": None},
        {"id": "think", "kind": "work"},
        {"id": "fetch", "kind": "script", "command": "echo partial; echo boom >&2; exit 3"},
    ]})
    res = _engine(tmp_path).run(wf, "t")
    assert res.status == "completed"                 # the surviving branch carries the node
    fetch = _row(res, "fetch")
    assert fetch.status == "node_failed" and fetch.exit_code == 3
    assert "exit 3" in fetch.command
    assert fetch.stdout.strip() == "partial"
    assert "boom" in fetch.stderr


def test_cut_script_branch_output_is_marked_and_refused_as_a_fan_out_list(tmp_path):
    wf = parse_workflow({"name": "w", "start": "prep", "nodes": [
        {"id": "prep", "kind": "parallel", "branches": ["ids", "think"],
         "reconcile": "read", "next": "fan"},
        {"id": "ids", "kind": "script",
         "command": "python3 -c \"import json; print(json.dumps(['item-%d' % i for i in range(40)]))\""},
        {"id": "think", "kind": "work"},
        {"id": "fan", "kind": "parallel", "worker": "dev", "list_from": "ids", "next": None},
        {"id": "dev", "kind": "work"},
    ]})
    res = _engine(tmp_path, script={"max_output_chars": 60}).run(wf, "t")
    assert _row(res, "ids").output_truncated is True
    assert res.status == "aborted"
    assert "larger than the script output cap" in res.final_output
    assert not [r for r in res.runs if r.node_id == "dev"]      # no worker ran on a fragment


# ── static branches working in private copies (choose / union) ──

def _writing(reconcile, **extra):
    return parse_workflow({"name": "w", "start": "fan", "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["a", "b"],
         "reconcile": reconcile, "next": None, **extra},
        {"id": "a", "kind": "work", "tools": "default"},
        {"id": "b", "kind": "script", "command": "echo from-b > b.txt; echo b-done"},
    ]})


def test_choose_branches_record_producer_evidence_and_duration(tmp_path):
    res = _engine(tmp_path, pick_runner=lambda criteria, options, model: 0).run(
        _writing("choose", criteria="best"), "t")
    assert res.status == "completed"
    a, b = _row(res, "a"), _row(res, "b")
    assert (a.model, a.provider) == ("model-x", "provider-y") and a.node_hash
    assert "echo from-b" in b.command and b.stdout.strip() == "b-done" and b.exit_code == 0
    assert a.duration_s is not None and b.duration_s is not None


def test_union_branch_whose_session_save_failed_says_so(tmp_path):
    def runner(req):
        return NodeRunResponse(output="x", session_key=None, persist_failed=True,
                               model="model-x", provider="provider-y")

    res = _engine(tmp_path, node_runner=runner).run(_writing("union"), "t")
    assert _row(res, "a").status == "persist_failed"


def test_union_branch_that_fails_leaves_a_row_for_every_branch(tmp_path):
    wf = parse_workflow({"name": "w", "start": "fan", "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["a", "b"],
         "reconcile": "union", "next": None},
        {"id": "a", "kind": "work", "tools": "default"},
        {"id": "b", "kind": "script", "command": "echo half; echo broke >&2; exit 4"},
    ]})
    res = _engine(tmp_path).run(wf, "t")
    assert res.status == "aborted" and res.failed_node == "b"
    b = _row(res, "b")
    assert b.status == "node_failed" and b.exit_code == 4 and b.branch_id == "b"
    assert "exit 4" in b.command and "broke" in b.stderr
    a = _row(res, "a")                               # the branch that finished is on record too
    assert a.status == "ok" and a.model == "model-x"


# ── fan-out workers ──

def test_fan_out_workers_record_their_model_provider_and_definition_hash(tmp_path):
    wf = parse_workflow({"name": "w", "start": "orch", "nodes": [
        {"id": "orch", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "worker": "dev", "list_from": "orch", "next": None},
        {"id": "dev", "kind": "work"},
    ]})

    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output='["one", "two", "three"]', model="model-x", provider="provider-y")
        return NodeRunResponse(output=f"did {req.task}", model="model-w", provider="provider-y")

    res = _engine(tmp_path, node_runner=runner).run(wf, "t")
    assert res.status == "completed"
    workers = [r for r in res.runs if r.node_id == "dev"]
    assert [w.worker_index for w in workers] == [0, 1, 2]
    assert [w.output for w in workers] == ["did one", "did two", "did three"]
    assert {(w.model, w.provider) for w in workers} == {("model-w", "provider-y")}
    assert all(w.node_hash for w in workers)


# ── detached nodes ──

def _detached(side):
    return parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "side"},
        {**side, "id": "side", "detached": True, "next": "b"},
        {"id": "b", "kind": "work", "next": None},
    ]})


def test_detached_agent_node_records_its_model_and_provider(tmp_path):
    res = _engine(tmp_path).run(_detached({"kind": "work"}), "t")
    side = _row(res, "side")
    assert (side.model, side.provider) == ("model-x", "provider-y") and side.node_hash


def test_detached_script_records_its_command_and_streams(tmp_path):
    res = _engine(tmp_path).run(_detached({"kind": "script", "command": "echo saved"}), "t")
    side = _row(res, "side")
    assert "echo saved" in side.command and side.stdout.strip() == "saved"


def test_failed_detached_script_keeps_what_it_ran_and_printed(tmp_path):
    res = _engine(tmp_path).run(
        _detached({"kind": "script", "command": "echo tried; echo denied >&2; exit 5"}), "t")
    assert res.status == "completed"                 # a side effect never sinks the run
    side = _row(res, "side")
    assert side.status == "node_failed" and side.exit_code == 5
    assert "exit 5" in side.command and side.stdout.strip() == "tried" and "denied" in side.stderr


# ── the durable record ──

def test_the_run_record_on_disk_carries_branch_evidence(tmp_path):
    _engine(tmp_path).run(_mixed(), "t")
    path = Path(tmp_path) / "workflows-runs" / "w" / "r1.json"
    by_node = {r["node_id"]: r for r in json.loads(path.read_text())["runs"]}
    assert by_node["think"]["model"] == "model-x"
    assert by_node["think"]["provider"] == "provider-y"
    assert by_node["think"]["node_hash"]
    assert "echo fetched" in by_node["fetch"]["command"]
    assert by_node["fetch"]["stdout"].strip() == "fetched"
    assert run_log.read_manifest(tmp_path, "w", "r1")["status"] == "completed"
