"""A multi-way gate can route to the reserved ``__needs_input__`` target: the run ends
with status ``needs_input`` carrying the node's output (the questions) instead of
completing. The human-in-the-loop lives in the agent that invoked the workflow — it asks
the user and re-runs — so the engine never blocks for input or touches the user channel."""

from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def _wf():
    return parse_workflow({"name": "w", "start": "ask", "max_visits": 3, "nodes": [
        {"id": "ask", "kind": "work",
         "cases": {"READY": "do", "NEED_INFO": "__needs_input__"}},
        {"id": "do", "kind": "work", "next": None}]})


def test_route_to_needs_input_ends_with_that_status_and_carries_questions():
    def runner(req):
        return NodeRunResponse(output="I need to know X and Y.\nNEED_INFO")

    res = WorkflowEngine(runner, run_id_factory=lambda: "r1").run(_wf(), "go")
    assert res.status == "needs_input"
    assert "X and Y" in (res.final_output or "")   # the questions ride the output
    assert not any(r.node_id == "do" for r in res.runs)   # the downstream node never ran


def test_route_to_a_real_target_still_completes():
    def runner(req):
        if req.node.id == "ask":
            return NodeRunResponse(output="all set\nREADY")
        return NodeRunResponse(output="done")

    res = WorkflowEngine(runner, run_id_factory=lambda: "r1").run(_wf(), "go")
    assert res.status == "completed"


def test_reserved_target_passes_spec_validation():
    # __needs_input__ is a reserved routing target, exempt from the unknown-node check;
    # a real-but-undefined target is still rejected.
    parse_workflow({"name": "w", "start": "g", "nodes": [
        {"id": "g", "kind": "work", "cases": {"OK": None, "ASK": "__needs_input__"}}]})

    import pytest

    from durin.workflow.spec import WorkflowError
    with pytest.raises(WorkflowError):
        parse_workflow({"name": "w", "start": "g", "nodes": [
            {"id": "g", "kind": "work", "cases": {"OK": None, "ASK": "nope"}}]})
