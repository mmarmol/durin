"""``branches_from``: a parallel node whose branch set is chosen at runtime.

Live finding (mxHero box, 2026-07-22): expressing "run only the analyzers that
apply to this ticket's attachment types" required EIGHT static parallel nodes —
the powerset of three analyzers — because ``cases`` routes to one node and
``branches`` is fixed. ``branches_from`` names the node whose output lists the
branch ids to run this pass (typically a routing script), collapsing the
combinatorial blocks into one parallel node.
"""

import pytest

from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.spec import WorkflowError, parse_workflow


def _wf(extra_parallel=None, branch_defs=None):
    parallel = {"id": "fan", "kind": "parallel", "branches_from": "route",
                "next": "after", **(extra_parallel or {})}
    nodes = [
        {"id": "route", "kind": "work", "next": "fan"},
        parallel,
        *(branch_defs if branch_defs is not None else [
            {"id": "b1", "kind": "work"},
            {"id": "b2", "kind": "work"},
            {"id": "b3", "kind": "work"},
        ]),
        {"id": "after", "kind": "work", "next": None},
    ]
    return parse_workflow({"name": "w", "start": "route", "nodes": nodes})


def _runner(route_output, seen=None):
    def nr(req):
        if req.node.id == "route":
            return NodeRunResponse(output=route_output)
        if seen is not None:
            seen.append(req.node.id)
        return NodeRunResponse(output=f"{req.node.id}-out")
    return nr


# ── spec ──

def test_branches_from_is_exclusive_with_static_branches():
    with pytest.raises(WorkflowError, match="branches_from"):
        parse_workflow({"name": "w", "start": "p", "nodes": [
            {"id": "p", "kind": "parallel", "branches": ["a"], "branches_from": "r"},
            {"id": "a", "kind": "work"},
            {"id": "r", "kind": "work"},
        ]})


def test_branches_from_is_exclusive_with_dynamic_worker():
    with pytest.raises(WorkflowError, match="branches_from"):
        parse_workflow({"name": "w", "start": "p", "nodes": [
            {"id": "p", "kind": "parallel", "worker": "a", "list_from": "r",
             "branches_from": "r"},
            {"id": "a", "kind": "work"},
            {"id": "r", "kind": "work"},
        ]})


def test_branches_from_must_reference_an_existing_node():
    with pytest.raises(WorkflowError, match="ghost"):
        parse_workflow({"name": "w", "start": "p", "nodes": [
            {"id": "p", "kind": "parallel", "branches_from": "ghost"},
            {"id": "a", "kind": "work"},
        ]})


def test_parallel_still_requires_some_branch_mode():
    with pytest.raises(WorkflowError, match="branches"):
        parse_workflow({"name": "w", "start": "p", "nodes": [
            {"id": "p", "kind": "parallel"},
        ]})


# ── engine ──

def test_runs_exactly_the_branches_the_source_named_json_form():
    seen = []
    eng = WorkflowEngine(node_runner=_runner('["b1", "b3"]', seen),
                         run_id_factory=lambda: "r1")
    res = eng.run(_wf(), "t")
    assert res.status == "completed"
    assert sorted(seen) == ["after", "b1", "b3"]      # b2 not run
    fan = next(r for r in res.runs if r.node_id == "fan")
    assert "[b1]" in fan.output and "[b3]" in fan.output and "[b2]" not in fan.output


def test_comma_separated_last_line_form():
    seen = []
    eng = WorkflowEngine(node_runner=_runner("routing done\nb2, b3", seen),
                         run_id_factory=lambda: "r1")
    res = eng.run(_wf(), "t")
    assert res.status == "completed"
    assert sorted(seen) == ["after", "b2", "b3"]


def test_repeated_ids_are_deduped_preserving_order():
    seen = []
    eng = WorkflowEngine(node_runner=_runner('["b1", "b1", "b2"]', seen),
                         run_id_factory=lambda: "r1")
    res = eng.run(_wf(), "t")
    assert res.status == "completed"
    assert sorted(x for x in seen if x != "after") == ["b1", "b2"]


def test_empty_list_skips_the_fan_out_and_continues():
    seen = []
    eng = WorkflowEngine(node_runner=_runner("[]", seen),
                         run_id_factory=lambda: "r1")
    res = eng.run(_wf(), "t")
    assert res.status == "completed"
    assert seen == ["after"]                          # no branch ran; walk continued
    fan = next(r for r in res.runs if r.node_id == "fan")
    assert fan.output == ""


def test_unknown_branch_id_aborts_naming_it():
    eng = WorkflowEngine(node_runner=_runner('["b1", "nope"]'),
                         run_id_factory=lambda: "r1")
    res = eng.run(_wf(), "t")
    assert res.status == "aborted"
    assert "nope" in (res.final_output or "")


def test_branch_resolving_to_non_work_node_aborts():
    # 'route' exists but a parallel branch must be a work node — naming another
    # kind at runtime is the same authoring error as naming it statically.
    wf = parse_workflow({"name": "w", "start": "route", "nodes": [
        {"id": "route", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches_from": "route", "next": None},
        {"id": "sub", "kind": "subworkflow", "workflow": "x"},
        {"id": "b1", "kind": "work"},
    ]})
    eng = WorkflowEngine(node_runner=_runner('["sub"]'), run_id_factory=lambda: "r1",
                         subworkflow_runner=lambda *a, **k: "x")
    res = eng.run(wf, "t")
    assert res.status == "aborted"
    assert "sub" in (res.final_output or "")


def test_persistent_session_branch_is_rejected_at_runtime():
    wf = _wf(branch_defs=[
        {"id": "b1", "kind": "work", "session": "persistent", "context": "own"},
        {"id": "b2", "kind": "work"},
        {"id": "b3", "kind": "work"},
    ])
    eng = WorkflowEngine(node_runner=_runner('["b1"]'), run_id_factory=lambda: "r1")
    res = eng.run(wf, "t")
    assert res.status == "aborted"
    assert "persistent" in (res.final_output or "")
