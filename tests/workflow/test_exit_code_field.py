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


def test_manifest_carries_node_failed_error(tmp_path):
    from durin.workflow import run_log
    from durin.workflow.result import NodeRun, WorkflowResult
    r = NodeRun(node_id="s", iteration=1, output="", status="node_failed",
                error="script exited with code 2: boom", exit_code=2)
    result = WorkflowResult(status="aborted", final_output=None, runs=[r], run_id="r9")
    run_log.start_run(tmp_path, "wf", "r9", root_session_key="k", started_at=1.0)
    run_log.finalize_run(tmp_path, "wf", result, root_session_key="k", started_at=1.0, finished_at=2.0)
    row = run_log.read_manifest(tmp_path, "wf", "r9")["runs"][0]
    assert row["error"] == "script exited with code 2: boom"
    assert row["exit_code"] == 2


def test_manifest_error_is_none_for_ok_rows(tmp_path):
    from durin.workflow import run_log
    from durin.workflow.result import NodeRun, WorkflowResult
    r = NodeRun(node_id="s", iteration=1, output="fine")
    result = WorkflowResult(status="completed", final_output="fine", runs=[r], run_id="ra")
    run_log.start_run(tmp_path, "wf", "ra", root_session_key="k", started_at=1.0)
    run_log.finalize_run(tmp_path, "wf", result, root_session_key="k", started_at=1.0, finished_at=2.0)
    assert run_log.read_manifest(tmp_path, "wf", "ra")["runs"][0]["error"] is None
