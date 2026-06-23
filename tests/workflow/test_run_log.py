"""Tests for per-run workflow records (the diagnostic source)."""

from durin.workflow import run_log
from durin.workflow.result import NodeRun, WorkflowResult


def _result(run_id, status="completed", runs=None):
    return WorkflowResult(status=status, final_output="x", runs=runs or [], run_id=run_id)


def test_write_and_read_run_round_trips(tmp_path):
    res = _result("r1", runs=[
        NodeRun(node_id="a", iteration=2, output="o"),
        NodeRun(node_id="g", iteration=1, output="", passed=False),
    ])
    run_log.write_run(tmp_path, "wf", res, ts=100.0)
    got = run_log.read_runs_since(tmp_path, "wf")
    assert len(got) == 1
    rec = got[0]
    assert rec["run_id"] == "r1" and rec["status"] == "completed"
    assert {"node_id": "a", "iteration": 2, "passed": None} in rec["runs"]
    assert {"node_id": "g", "iteration": 1, "passed": False} in rec["runs"]


def test_records_land_beside_workflows_not_inside(tmp_path):
    (tmp_path / "workflows").mkdir()
    run_log.write_run(tmp_path, "wf", _result("r1"), ts=1.0)
    # the version-store snapshots <workspace>/workflows; run records must not be there
    assert not list((tmp_path / "workflows").glob("**/*.json"))
    assert (tmp_path / "workflows-runs" / "wf" / "r1.json").exists()


def test_cursor_excludes_consumed_runs(tmp_path):
    run_log.write_run(tmp_path, "wf", _result("r1"), ts=10.0)
    run_log.write_run(tmp_path, "wf", _result("r2"), ts=20.0)
    run_log.advance_cursor(tmp_path, "wf", 10.0)
    fresh = run_log.read_runs_since(tmp_path, "wf", run_log.read_cursor(tmp_path, "wf"))
    assert [r["run_id"] for r in fresh] == ["r2"]   # r1 already consumed


def test_read_runs_sorted_oldest_first(tmp_path):
    run_log.write_run(tmp_path, "wf", _result("late"), ts=30.0)
    run_log.write_run(tmp_path, "wf", _result("early"), ts=5.0)
    assert [r["run_id"] for r in run_log.read_runs_since(tmp_path, "wf")] == ["early", "late"]


def test_names_with_runs(tmp_path):
    run_log.write_run(tmp_path, "alpha", _result("r1"), ts=1.0)
    run_log.write_run(tmp_path, "beta", _result("r2"), ts=1.0)
    assert run_log.workflow_names_with_runs(tmp_path) == ["alpha", "beta"]


def test_no_runs_is_empty(tmp_path):
    assert run_log.read_runs_since(tmp_path, "nope") == []
    assert run_log.read_cursor(tmp_path, "nope") == 0.0
    assert run_log.workflow_names_with_runs(tmp_path) == []
