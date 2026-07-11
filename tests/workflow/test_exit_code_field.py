"""Tests for exit_code field in node response, trace, and manifest."""

from durin.workflow import run_log
from durin.workflow.result import NodeRun, WorkflowResult


def test_node_run_and_manifest_carry_exit_code(tmp_path):
    r = NodeRun(node_id="s", iteration=1, output="ok", exit_code=0)
    assert r.exit_code == 0
    result = WorkflowResult(status="completed", final_output="ok", runs=[r], run_id="r1")
    run_log.start_run(tmp_path, "wf", "r1", root_session_key="k", started_at=1.0)
    run_log.finalize_run(tmp_path, "wf", result, root_session_key="k", started_at=1.0, finished_at=2.0)
    manifest = run_log.read_manifest(tmp_path, "wf", "r1")
    assert manifest["runs"][0]["exit_code"] == 0
