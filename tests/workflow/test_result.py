"""Tests for the typed workflow result."""

from durin.workflow.result import NodeRun, WorkflowResult


def test_node_run_defaults():
    r = NodeRun(node_id="a", iteration=1, output="done")
    assert r.session_key is None
    assert r.passed is None


def test_workflow_result_shape():
    res = WorkflowResult(
        status="completed",
        final_output="ok",
        runs=[NodeRun(node_id="a", iteration=1, output="ok", session_key="workflow:r1:a:1")],
        run_id="r1",
    )
    assert res.status == "completed"
    assert res.final_output == "ok"
    assert res.runs[0].session_key == "workflow:r1:a:1"
    assert res.run_id == "r1"
