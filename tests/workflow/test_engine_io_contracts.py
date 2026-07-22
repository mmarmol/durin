"""Named inputs (`inputs_from`) and schema-validated outputs (`output_schema`).

Live motivation (mxHero box, 2026-07-22): the edge is ONE string that linear
script nodes replace — stage1's resolve-org script had to save the ticket
analysis to a courier file by hand, and a downstream prompt claimed to receive
text it never saw. And JSON contracts were validated by downstream script
gates, where a malformed payload costs a full loop-back instead of an
immediate in-node retry.

`inputs_from`: the node's input becomes labeled blocks `[source]\\n<output>`
(last recorded execution of each named source) plus an `[upstream]` block that
always carries the walk's current edge text — loop-back feedback and route
context are never lost. `output_schema`: the runner delivers a validated
payload (fake runners here honor the contract by returning JSON); the ENGINE
writes `output_file` from it, so the file cannot be malformed.
"""

import json

import pytest

from durin.workflow.engine import NodeRunResponse, ResumeState, WorkflowEngine
from durin.workflow.spec import WorkflowError, parse_workflow


# ── spec: inputs_from ──

def test_inputs_from_parses_on_work_and_script():
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "script", "command": "cat", "inputs_from": ["a"], "next": "c"},
        {"id": "c", "kind": "work", "inputs_from": ["a", "b"], "next": None},
    ]})
    assert wf.nodes["b"].inputs_from == ("a",)
    assert wf.nodes["c"].inputs_from == ("a", "b")


def test_inputs_from_must_reference_existing_nodes():
    with pytest.raises(WorkflowError, match="ghost"):
        parse_workflow({"name": "d", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "inputs_from": ["ghost"], "next": None},
        ]})


def test_inputs_from_rejects_self_and_detached_sources():
    with pytest.raises(WorkflowError, match="itself"):
        parse_workflow({"name": "d", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "inputs_from": ["a"], "next": None},
        ]})
    with pytest.raises(WorkflowError, match="detached"):
        parse_workflow({"name": "d", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "next": "side"},
            {"id": "side", "kind": "work", "detached": True, "next": "b"},
            {"id": "b", "kind": "work", "inputs_from": ["side"], "next": None},
        ]})


# ── spec: output_schema / output_file ──

def test_output_schema_parses_and_output_file_requires_it():
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work",
         "output_schema": {"type": "object", "required": ["x"],
                           "properties": {"x": {"type": "string"}}},
         "output_file": "a.json", "next": None},
    ]})
    assert wf.nodes["a"].output_schema["required"] == ["x"]
    assert wf.nodes["a"].output_file == "a.json"
    with pytest.raises(WorkflowError, match="output_file"):
        parse_workflow({"name": "d", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "output_file": "a.json", "next": None},
        ]})


def test_output_schema_rejects_routing_and_bad_paths():
    with pytest.raises(WorkflowError, match="rout"):
        parse_workflow({"name": "d", "start": "a", "nodes": [
            {"id": "a", "kind": "work",
             "output_schema": {"type": "object"}, "on_pass": None, "on_fail": "a"},
        ]})
    with pytest.raises(WorkflowError, match="output_file"):
        parse_workflow({"name": "d", "start": "a", "nodes": [
            {"id": "a", "kind": "work", "output_schema": {"type": "object"},
             "output_file": "../escape.json", "next": None},
        ]})


def test_invalid_schema_document_is_rejected():
    with pytest.raises(WorkflowError, match="output_schema"):
        parse_workflow({"name": "d", "start": "a", "nodes": [
            {"id": "a", "kind": "work",
             "output_schema": {"type": "not-a-type"}, "next": None},
        ]})


# ── engine: composition ──

def _seen_runner(outputs, seen):
    def node_runner(req):
        seen[req.node.id] = req.upstream_output
        return NodeRunResponse(output=outputs.get(req.node.id, f"{req.node.id}-out"))
    return node_runner


def test_composed_input_carries_labeled_sources_and_upstream():
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": "c"},
        {"id": "c", "kind": "work", "inputs_from": ["a"], "next": None},
    ]})
    seen = {}
    eng = WorkflowEngine(node_runner=_seen_runner({}, seen), run_id_factory=lambda: "r1")
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert "[a]\na-out" in seen["c"]
    assert "[upstream]\nb-out" in seen["c"]      # the edge is always the last block


def test_missing_source_composes_as_marker_not_error():
    wf = parse_workflow({"name": "d", "start": "route", "nodes": [
        {"id": "route", "kind": "work", "cases": {"L": "left", "R": "right"}},
        {"id": "left", "kind": "work", "next": "join"},
        {"id": "right", "kind": "work", "next": "join"},
        {"id": "join", "kind": "work", "inputs_from": ["left", "right"], "next": None},
    ]})
    seen = {}

    def node_runner(req):
        seen[req.node.id] = req.upstream_output
        return NodeRunResponse(output="L" if req.node.id == "route" else f"{req.node.id}-out")

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")
    assert eng.run(wf, "t").status == "completed"
    assert "[left]\nleft-out" in seen["join"]
    assert "[right]\n(no output recorded)" in seen["join"]


def test_script_node_receives_composed_input_on_stdin(tmp_path):
    d = tmp_path / "workflows" / "scripts"
    d.mkdir(parents=True)
    (d / "echo.py").write_text("import sys\nprint(sys.stdin.read())\n")
    from durin.workflow.script_runner import ScriptNodeRunner
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": "s"},
        {"id": "s", "kind": "script", "script": "echo.py", "inputs_from": ["a"], "next": None},
    ]})

    def node_runner(req):
        return NodeRunResponse(output=f"{req.node.id}-out")

    eng = WorkflowEngine(node_runner=node_runner, script_runner=ScriptNodeRunner(tmp_path),
                         run_id_factory=lambda: "r1", workspace=str(tmp_path))
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert "[a]\na-out" in res.final_output
    assert "[upstream]\nb-out" in res.final_output


def test_resume_seeds_source_outputs_for_composition():
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": "c"},
        {"id": "c", "kind": "work", "inputs_from": ["a"], "next": None},
    ]})
    seen = {}
    eng = WorkflowEngine(node_runner=_seen_runner({}, seen), run_id_factory=lambda: "r1")
    resume = ResumeState(run_id="r1", start_at="c", visits={"a": 1, "b": 1},
                         upstream="b-out", recorded_outputs={"a": "a-prior"})
    res = eng.run(wf, "t", resume=resume)
    assert res.status == "completed"
    assert "[a]\na-prior" in seen["c"]           # composed from the seeded record


# ── engine: output_file ──

def test_engine_writes_the_validated_payload_file(tmp_path):
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work",
         "output_schema": {"type": "object", "required": ["queries"],
                           "properties": {"queries": {"type": "array"}}},
         "output_file": "plan.json", "next": None},
    ]})

    def node_runner(req):
        return NodeRunResponse(output=json.dumps({"queries": ["q1", "q2"]}))

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                         workspace=str(tmp_path))
    res = eng.run(wf, "t")
    assert res.status == "completed"
    written = json.loads((tmp_path / ".workflow" / "r1" / "work" / "plan.json").read_text())
    assert written == {"queries": ["q1", "q2"]}
