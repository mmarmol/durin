"""Tests for WorkflowEngine progress_emit callback.

The engine calls ``progress_emit`` after each node record (and update_manifest)
so the caller can observe partial run state in real time.
"""

from durin.workflow.engine import NodeRunRequest, NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def _make_runner(outputs: dict):
    """Return a node runner scripted to produce the given outputs."""
    def runner(req: NodeRunRequest) -> NodeRunResponse:
        return NodeRunResponse(
            output=outputs[req.node.id],
            session_key=f"workflow:{req.run_id}:{req.node.id}:{req.iteration}",
            messages=[],
        )
    return runner


def test_engine_calls_progress_emit_with_accumulated_nodes(tmp_path):
    calls = []
    wf = parse_workflow({
        "name": "prog", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })
    eng = WorkflowEngine(
        node_runner=_make_runner({"a": "out-a", "b": "out-b"}),
        run_id_factory=lambda: "r1",
        progress_emit=lambda p: calls.append(p),
    )
    result = eng.run(wf, "do it", root_session_key="websocket:chatA")
    assert result.status == "completed"
    assert calls, "progress_emit never called"
    # Must be called at least once per node.
    assert len(calls) >= 2
    # Each call has the required keys.
    for call in calls:
        assert "run_id" in call
        assert "nodes" in call
        assert "done" in call
    # Last call carries both nodes.
    last = calls[-1]
    assert {n["id"] for n in last["nodes"]} == {"a", "b"}
    # All nodes in the last call are "done".
    assert all(n["status"] == "done" for n in last["nodes"])


def test_engine_progress_emit_not_required():
    """Engine works fine without a progress_emit (backward compat)."""
    wf = parse_workflow({
        "name": "noprog", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "next": None}],
    })
    eng = WorkflowEngine(node_runner=_make_runner({"a": "ok"}), run_id_factory=lambda: "r1")
    result = eng.run(wf, "go")
    assert result.status == "completed"


def test_engine_progress_emit_exception_does_not_break_run():
    """A crashing progress_emit must not abort the run."""
    wf = parse_workflow({
        "name": "crashprog", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })

    def _bad_emit(payload):
        raise RuntimeError("emit failed")

    eng = WorkflowEngine(
        node_runner=_make_runner({"a": "x", "b": "y"}),
        run_id_factory=lambda: "r1",
        progress_emit=_bad_emit,
    )
    result = eng.run(wf, "do it")
    assert result.status == "completed"
