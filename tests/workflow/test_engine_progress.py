"""Tests for WorkflowEngine progress_emit callback.

The engine calls ``progress_emit`` after each node record (and update_manifest)
so the caller can observe partial run state in real time.  For parallel nodes it
also emits per-branch progress frames so the UI can show each branch advancing
live rather than waiting for all branches to finish.
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


def test_parallel_node_emits_branch_progress():
    """A parallel node must emit frames with per-branch status as branches finish.

    Specifically:
    - At least one frame must carry a 'branches' list on the parallel node entry.
    - The final branch statuses must include 'done' (branches completed).
    """
    frames = []

    # A minimal workflow: one work node feeds into a parallel gather node with two
    # branches (reconcile='read' is the simplest path — no workspace needed).
    wf = parse_workflow({
        "name": "br-prog", "start": "pre",
        "nodes": [
            {"id": "pre", "kind": "work", "next": "gather"},
            {"id": "gather", "kind": "parallel", "branches": ["br1", "br2"], "next": None},
            {"id": "br1", "kind": "work"},
            {"id": "br2", "kind": "work"},
        ],
    })

    eng = WorkflowEngine(
        node_runner=_make_runner({"pre": "ctx", "br1": "out1", "br2": "out2"}),
        run_id_factory=lambda: "r1",
        progress_emit=frames.append,
    )
    result = eng.run(wf, "x", root_session_key=None)
    assert result.status == "completed"

    # At least one frame must carry branches on the parallel node.
    branch_frames = [
        f for f in frames
        for n in f["nodes"]
        if n.get("branches")
    ]
    assert branch_frames, "expected at least one frame carrying a node's 'branches' list"

    # Collect all branch statuses across all branch-carrying frames.
    statuses = {
        b["status"]
        for f in branch_frames
        for n in f["nodes"]
        for b in (n.get("branches") or [])
    }
    assert "done" in statuses, f"expected 'done' in branch statuses; got {statuses}"
