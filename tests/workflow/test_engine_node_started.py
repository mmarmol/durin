"""Tests for the node-started progress_emit frame.

Before a WorkNode executes, the engine emits a progress frame with prior
nodes as done/failed plus the about-to-run node as ``status:"running"``.
This frame lets the frontend show a spinner on the in-flight node before
it finishes.
"""

from durin.workflow.engine import NodeRunRequest, NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def test_node_started_emit_carries_running_status_before_noderun_appended():
    """progress_emit must be called with the current node as 'running' before it finishes."""
    emit_calls = []
    # Snapshots of emit_calls captured at the moment each node runner fires.
    snapshots_at_runner_call = {}

    wf = parse_workflow({
        "name": "spinner", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        # Record the full list of emit calls seen at the time this node starts.
        snapshots_at_runner_call[req.node.id] = list(emit_calls)
        return NodeRunResponse(
            output=f"out-{req.node.id}",
            session_key=f"workflow:{req.run_id}:{req.node.id}:{req.iteration}",
            messages=[],
        )

    eng = WorkflowEngine(
        node_runner=runner,
        run_id_factory=lambda: "r1",
        progress_emit=lambda p: emit_calls.append(p),
    )
    result = eng.run(wf, "do it", root_session_key="websocket:chatA")
    assert result.status == "completed"

    # At the moment node "a"'s runner was called, at least one emit should have
    # already arrived with node "a" marked as "running".
    calls_before_a = snapshots_at_runner_call["a"]
    assert calls_before_a, "No progress_emit fired before node 'a' runner was called"
    running_frames_for_a = [
        c for c in calls_before_a
        if any(n["id"] == "a" and n["status"] == "running" for n in c["nodes"])
    ]
    assert running_frames_for_a, (
        "Expected a progress_emit with node 'a' as 'running' before its NodeRun was appended; "
        f"got emit calls: {calls_before_a}"
    )

    # At the moment node "b"'s runner was called, a "running" frame for "b"
    # must exist and "a" must already appear as "done".
    calls_before_b = snapshots_at_runner_call["b"]
    running_frames_for_b = [
        c for c in calls_before_b
        if any(n["id"] == "b" and n["status"] == "running" for n in c["nodes"])
    ]
    assert running_frames_for_b, (
        "Expected a progress_emit with node 'b' as 'running' before its NodeRun was appended; "
        f"got emit calls before b: {calls_before_b}"
    )
    # In that same frame, "a" must be done (its NodeRun was already appended).
    b_running_frame = running_frames_for_b[-1]
    a_entries = [n for n in b_running_frame["nodes"] if n["id"] == "a"]
    assert a_entries and a_entries[0]["status"] == "done", (
        f"Expected node 'a' to be 'done' in the running frame for 'b'; frame: {b_running_frame}"
    )
    # The running frame must NOT yet contain "b" as done (it hasn't finished).
    b_done_in_frame = any(
        n["id"] == "b" and n["status"] == "done" for n in b_running_frame["nodes"]
    )
    assert not b_done_in_frame, "Node 'b' must not be 'done' in its own running frame"
