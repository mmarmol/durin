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

def test_branches_alongside_branches_from_declares_the_candidate_pool():
    # No longer exclusive: `branches` with `branches_from` is the declared
    # candidate pool (runtime ids validated against it; the editor draws the
    # candidates connected). See TestDeclaredCandidatePool below.
    wf = parse_workflow({"name": "w", "start": "p", "nodes": [
        {"id": "p", "kind": "parallel", "branches": ["a"], "branches_from": "r", "next": None},
        {"id": "a", "kind": "work"},
        {"id": "r", "kind": "work"},
    ]})
    assert wf.nodes["p"].branches == ("a",)
    assert wf.nodes["p"].branches_from == "r"


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


class TestDeclaredCandidatePool:
    """`branches` alongside `branches_from` declares the candidate pool: the
    editor can draw the runtime branches connected, and the engine validates
    resolved ids against the pool instead of accepting any work/script node."""

    def _wf(self, pool):
        from durin.workflow.spec import parse_workflow
        nodes = [
            {"id": "route", "kind": "work", "next": "fan"},
            {"id": "fan", "kind": "parallel", "branches_from": "route",
             **({"branches": pool} if pool is not None else {}),
             "reconcile": "read", "next": "join"},
            {"id": "a", "kind": "work"},
            {"id": "b", "kind": "work"},
            {"id": "c", "kind": "work"},
            {"id": "join", "kind": "work", "next": None},
        ]
        return parse_workflow({"name": "d", "start": "route", "nodes": nodes})

    def test_pool_parses_alongside_branches_from(self):
        wf = self._wf(["a", "b"])
        assert wf.nodes["fan"].branches == ("a", "b")
        assert wf.nodes["fan"].branches_from == "route"

    def test_pool_members_must_exist_and_be_branchable(self):
        import pytest

        from durin.workflow.spec import WorkflowError, parse_workflow
        with pytest.raises(WorkflowError):
            parse_workflow({"name": "d", "start": "route", "nodes": [
                {"id": "route", "kind": "work", "next": "fan"},
                {"id": "fan", "kind": "parallel", "branches_from": "route",
                 "branches": ["ghost"], "reconcile": "read", "next": None},
            ]})

    def test_resolved_ids_outside_the_pool_abort_the_run(self, tmp_path):
        from durin.workflow.engine import NodeRunResponse, WorkflowEngine

        def node_runner(req):
            if req.node.id == "route":
                return NodeRunResponse(output='["a", "c"]', session_key=None, messages=[])
            return NodeRunResponse(output=f"{req.node.id}-out", session_key=None, messages=[])

        engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                                workspace=str(tmp_path))
        res = engine.run(self._wf(["a", "b"]), "t")
        assert res.status == "aborted"
        assert "c" in (res.final_output or "") and "pool" in (res.final_output or "").lower()

    def test_resolved_ids_inside_the_pool_run(self, tmp_path):
        from durin.workflow.engine import NodeRunResponse, WorkflowEngine

        def node_runner(req):
            if req.node.id == "route":
                return NodeRunResponse(output='["a", "b"]', session_key=None, messages=[])
            return NodeRunResponse(output=f"{req.node.id}-out", session_key=None, messages=[])

        engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                                workspace=str(tmp_path))
        res = engine.run(self._wf(["a", "b"]), "t")
        assert res.status == "completed"
        fan = next(r for r in res.runs if r.node_id == "fan")
        assert "[a]" in fan.output and "[b]" in fan.output

    def test_without_pool_current_behavior_stands(self, tmp_path):
        from durin.workflow.engine import NodeRunResponse, WorkflowEngine

        def node_runner(req):
            if req.node.id == "route":
                return NodeRunResponse(output='["a", "c"]', session_key=None, messages=[])
            return NodeRunResponse(output=f"{req.node.id}-out", session_key=None, messages=[])

        engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                                workspace=str(tmp_path))
        res = engine.run(self._wf(None), "t")
        assert res.status == "completed"
