from types import SimpleNamespace

from durin.workflow import run_log


def _finish(tmp_path, name, run_id, rows, status="completed"):
    result = SimpleNamespace(
        run_id=run_id, status=status, final_output="", final_output_node=None,
        needs_input_node=None, output_files=[], missing_artifacts=[],
        runs=[SimpleNamespace(
            node_id=n, iteration=1, passed=None, session_key=None, worker_index=None,
            branch_id=None, budget=None, status="ok", route_label=None, exit_code=None,
            duration_s=d, error=None,
        ) for n, d in rows],
    )
    run_log.finalize_run(tmp_path, name, result, root_session_key=None,
                         started_at=0.0, finished_at=1.0)


def test_typical_is_the_median_across_completed_runs(tmp_path):
    _finish(tmp_path, "wf", "r1", [("scan", 10.0)])
    _finish(tmp_path, "wf", "r2", [("scan", 20.0)])
    _finish(tmp_path, "wf", "r3", [("scan", 90.0)])
    assert run_log.typical_node_durations(tmp_path, "wf") == {"scan": 20.0}


def test_typical_ignores_runs_that_did_not_complete(tmp_path):
    _finish(tmp_path, "wf", "r1", [("scan", 10.0)])
    _finish(tmp_path, "wf", "r2", [("scan", 999.0)], status="aborted")
    assert run_log.typical_node_durations(tmp_path, "wf") == {"scan": 10.0}


def test_typical_skips_nodes_without_a_duration(tmp_path):
    _finish(tmp_path, "wf", "r1", [("scan", 10.0), ("gate", None)])
    assert run_log.typical_node_durations(tmp_path, "wf") == {"scan": 10.0}


def test_typical_is_empty_for_a_workflow_with_no_history(tmp_path):
    assert run_log.typical_node_durations(tmp_path, "never-run") == {}
