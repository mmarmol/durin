"""Cooperative cancellation: the engine checks ``cancel_check`` between nodes.

A cancel takes effect at the top of the node walk — a node already executing
finishes first, but the next node never starts. The run ends ``cancelled`` with
the partial per-node trace.
"""

from durin.workflow.engine import NodeRunRequest, NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def _wf_two_nodes():
    return parse_workflow({
        "name": "cancelme", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })


def test_cancel_after_first_node_stops_before_second():
    state = {"cancel": False}
    ran = []

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        ran.append(req.node.id)
        if req.node.id == "a":
            state["cancel"] = True  # ask to cancel once node a has run
        return NodeRunResponse(
            output=f"out-{req.node.id}",
            session_key=f"workflow:{req.run_id}:{req.node.id}:{req.iteration}",
            messages=[],
        )

    eng = WorkflowEngine(
        node_runner=runner,
        run_id_factory=lambda: "r1",
        cancel_check=lambda: state["cancel"],
    )
    result = eng.run(_wf_two_nodes(), "do it", root_session_key="websocket:chatA")

    assert result.status == "cancelled"
    assert ran == ["a"], "node b must never start once cancel is requested after a"
    assert [r.node_id for r in result.runs] == ["a"], "partial trace keeps node a"
    assert result.run_id == "r1"


def test_cancel_before_start_yields_empty_trace():
    def runner(req: NodeRunRequest) -> NodeRunResponse:  # pragma: no cover - never called
        raise AssertionError("no node should run when cancelled before start")

    eng = WorkflowEngine(
        node_runner=runner,
        run_id_factory=lambda: "r2",
        cancel_check=lambda: True,
    )
    result = eng.run(_wf_two_nodes(), "do it", root_session_key="websocket:chatA")

    assert result.status == "cancelled"
    assert result.runs == []


def test_no_cancel_check_completes_normally():
    def runner(req: NodeRunRequest) -> NodeRunResponse:
        return NodeRunResponse(
            output=f"out-{req.node.id}",
            session_key=f"workflow:{req.run_id}:{req.node.id}:{req.iteration}",
            messages=[],
        )

    eng = WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r3")
    result = eng.run(_wf_two_nodes(), "do it", root_session_key="websocket:chatA")
    assert result.status == "completed"
