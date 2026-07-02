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
    by_node = {(r["node_id"], r["iteration"]): r for r in rec["runs"]}
    assert by_node[("a", 2)]["passed"] is None
    assert by_node[("g", 1)]["passed"] is False


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


# --- live manifest (B1) -----------------------------------------------------


def test_start_run_writes_running_manifest(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key="sess:1", started_at=100.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec is not None
    assert rec["status"] == "running"
    assert rec["runs"] == []
    assert rec["root_session_key"] == "sess:1"
    assert rec["run_id"] == "r1"
    assert rec["started_at"] == 100.0


def test_update_run_reflects_node_records(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key="sess:1", started_at=100.0)
    res = _result("r1", status="running", runs=[
        NodeRun(node_id="a", iteration=1, output="o", session_key="workflow:r1:a:1", status="ok"),
        NodeRun(node_id="g", iteration=1, output="", passed=False,
                session_key="workflow:r1:g:1", status="ok"),
    ])
    run_log.update_run(tmp_path, "wf", "r1", res)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["status"] == "running"
    assert rec["root_session_key"] == "sess:1"   # preserved across update
    assert rec["started_at"] == 100.0            # preserved across update
    by_node = {r["node_id"]: r for r in rec["runs"]}
    assert by_node["a"]["session_key"] == "workflow:r1:a:1"
    assert by_node["a"]["status"] == "ok"
    assert by_node["g"]["passed"] is False


def test_finalize_run_writes_terminal_status(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key="sess:1", started_at=100.0)
    res = _result("r1", status="completed", runs=[
        NodeRun(node_id="a", iteration=1, output="o", session_key="workflow:r1:a:1"),
    ])
    run_log.finalize_run(tmp_path, "wf", res, root_session_key="sess:1",
                         started_at=100.0, finished_at=130.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["status"] == "completed"
    assert rec["finished_at"] == 130.0
    assert rec["runs"][0]["session_key"] == "workflow:r1:a:1"
    # the finalized record is dream-visible via read_runs_since (ts == finished_at)
    got = run_log.read_runs_since(tmp_path, "wf")
    assert [r["run_id"] for r in got] == ["r1"]


def test_finalize_records_needs_input_node(tmp_path):
    from durin.workflow.result import WorkflowResult
    result = WorkflowResult(status="needs_input", final_output="what env?",
                            runs=[], run_id="r9", needs_input_node="gate")
    run_log.finalize_run(tmp_path, "w", result, root_session_key=None,
                         started_at=1.0, finished_at=2.0)
    rec = run_log.read_manifest(tmp_path, "w", "r9")
    assert rec["needs_input_node"] == "gate"


def test_finalize_records_final_output_node(tmp_path):
    from durin.workflow.result import WorkflowResult
    result = WorkflowResult(status="completed", final_output="done", final_output_node="gate",
                            runs=[], run_id="r11")
    run_log.finalize_run(tmp_path, "w", result, root_session_key=None,
                         started_at=1.0, finished_at=2.0)
    rec = run_log.read_manifest(tmp_path, "w", "r11")
    assert rec["final_output_node"] == "gate"


def test_finalize_records_output_files(tmp_path):
    from durin.workflow.result import WorkflowResult
    result = WorkflowResult(status="completed", final_output="done",
                            runs=[], run_id="r10", output_files=["a.md"])
    run_log.finalize_run(tmp_path, "w", result, root_session_key=None,
                         started_at=1.0, finished_at=2.0)
    rec = run_log.read_manifest(tmp_path, "w", "r10")
    assert rec["output_files"] == ["a.md"]


def test_node_record_carries_budget(tmp_path):
    res = _result("r1", runs=[NodeRun(node_id="a", iteration=1, output="o", budget=3)])
    run_log.write_run(tmp_path, "wf", res, ts=100.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["runs"][0]["budget"] == 3


def test_runs_for_session_matches_root_newest_first(tmp_path):
    run_log.finalize_run(tmp_path, "wf", _result("old"), root_session_key="sess:1",
                         started_at=1.0, finished_at=2.0)
    run_log.finalize_run(tmp_path, "wf", _result("new"), root_session_key="sess:1",
                         started_at=10.0, finished_at=20.0)
    run_log.finalize_run(tmp_path, "other", _result("nope"), root_session_key="sess:2",
                         started_at=5.0, finished_at=6.0)
    got = run_log.runs_for_session(tmp_path, "sess:1")
    assert [r["run_id"] for r in got] == ["new", "old"]   # newest-first


def test_reconcile_marks_stale_running_as_crashed(tmp_path):
    run_log.start_run(tmp_path, "wf", "stale", root_session_key="s", started_at=0.0)
    run_log.start_run(tmp_path, "wf", "fresh", root_session_key="s", started_at=1950.0)
    run_log.finalize_run(tmp_path, "wf", _result("done"), root_session_key="s",
                         started_at=0.0, finished_at=5.0)

    n = run_log.reconcile_running(tmp_path, now=2000.0, max_age_s=100.0)
    assert n == 1   # only the stale running one

    assert run_log.read_manifest(tmp_path, "wf", "stale")["status"] == "crashed"
    assert run_log.read_manifest(tmp_path, "wf", "fresh")["status"] == "running"
    assert run_log.read_manifest(tmp_path, "wf", "done")["status"] == "completed"


def test_reconcile_preserves_partial_runs_and_survives_malformed(tmp_path):
    res = _result("stale", status="running", runs=[
        NodeRun(node_id="a", iteration=1, output="o", session_key="workflow:stale:a:1"),
    ])
    run_log.start_run(tmp_path, "wf", "stale", root_session_key="s", started_at=0.0)
    run_log.update_run(tmp_path, "wf", "stale", res)
    # A malformed record must not crash the sweep.
    (tmp_path / "workflows-runs" / "wf" / "junk.json").write_text("not json", encoding="utf-8")

    run_log.reconcile_running(tmp_path, now=2000.0, max_age_s=100.0)
    rec = run_log.read_manifest(tmp_path, "wf", "stale")
    assert rec["status"] == "crashed"
    assert rec["runs"][0]["session_key"] == "workflow:stale:a:1"   # partial trace kept



def test_task_persists_through_start_update_finalize(tmp_path):
    """The task written by start_run survives update_run and finalize_run."""
    run_log.start_run(tmp_path, "wf", "r1", root_session_key="sess:1",
                      started_at=100.0, task="summarise the quarterly report")
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["task"] == "summarise the quarterly report"

    res = _result("r1", status="running", runs=[
        NodeRun(node_id="a", iteration=1, output="o", session_key="sk", status="ok"),
    ])
    run_log.update_run(tmp_path, "wf", "r1", res)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["task"] == "summarise the quarterly report"

    run_log.finalize_run(tmp_path, "wf", _result("r1", runs=[
        NodeRun(node_id="a", iteration=1, output="o", session_key="sk"),
    ]), root_session_key="sess:1", started_at=100.0, finished_at=130.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["task"] == "summarise the quarterly report"
    assert rec["status"] == "completed"


def test_task_none_when_omitted(tmp_path):
    """start_run without task defaults to None, no task key in the record."""
    run_log.start_run(tmp_path, "wf", "r2", root_session_key=None, started_at=1.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r2")
    assert rec.get("task") is None


def test_read_runs_since_tolerates_old_schema(tmp_path):
    # A v1 on-disk record (no schema/root_session_key field, as written before the
    # manifest) is still returned by read_runs_since without error.
    import json
    d = tmp_path / "workflows-runs" / "wf"
    d.mkdir(parents=True)
    (d / "legacy.json").write_text(json.dumps({
        "run_id": "legacy", "workflow": "wf", "status": "completed", "ts": 50.0,
        "runs": [{"node_id": "a", "iteration": 1, "passed": None}],
    }), encoding="utf-8")
    got = run_log.read_runs_since(tmp_path, "wf")
    assert [r["run_id"] for r in got] == ["legacy"]
    assert "root_session_key" not in got[0]


def test_list_runs_newest_first_summaries(tmp_path):
    from durin.workflow.result import WorkflowResult
    run_log.finalize_run(tmp_path, "wf", WorkflowResult(
        status="completed", final_output="a" * 300, runs=[], run_id="old"),
        root_session_key=None, started_at=1.0, finished_at=2.0)
    run_log.finalize_run(tmp_path, "wf", WorkflowResult(
        status="needs_input", final_output="q", runs=[], run_id="new",
        needs_input_node="gate"),
        root_session_key=None, started_at=10.0, finished_at=20.0)
    got = run_log.list_runs(tmp_path, "wf")
    assert [r["run_id"] for r in got] == ["new", "old"]
    assert got[0]["status"] == "needs_input"
    assert got[0]["needs_input_node"] == "gate"
    assert got[1]["task"] == ""
    assert len(got[1]["task"]) <= 200


def test_list_runs_caps_task_at_200_chars(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key=None, started_at=1.0,
                      task="x" * 500)
    got = run_log.list_runs(tmp_path, "wf")
    assert len(got[0]["task"]) == 200


def test_list_runs_respects_limit(tmp_path):
    for i in range(3):
        run_log.start_run(tmp_path, "wf", f"r{i}", root_session_key=None,
                          started_at=float(i))
    got = run_log.list_runs(tmp_path, "wf", limit=2)
    assert len(got) == 2
    assert [r["run_id"] for r in got] == ["r2", "r1"]


def test_list_runs_no_directory_is_empty(tmp_path):
    assert run_log.list_runs(tmp_path, "nope") == []
