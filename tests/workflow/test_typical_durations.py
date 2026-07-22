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


def test_typical_total_does_not_sum_branches_no_single_run_takes(tmp_path):
    """The per-node medians span every branch prior runs took; one run takes one
    of them. Summing them estimates a path that cannot happen."""
    # Two prior runs of a router with two mutually exclusive branches.
    _finish(tmp_path, "router", "r1", [("route", 5.0), ("branch-a", 500.0)])
    _finish(tmp_path, "router", "r2", [("route", 5.0), ("branch-b", 520.0)])

    assert run_log.typical_total_duration(tmp_path, "router") == 515.0
    # What summing the per-node medians would have produced instead.
    assert sum(run_log.typical_node_durations(tmp_path, "router").values()) == 1025.0


def test_typical_total_counts_every_pass_of_a_looping_node(tmp_path):
    """A node visited three times contributes one median to typical_s but all
    three passes to the run's own total."""
    _finish(tmp_path, "loop", "r1", [("produce", 10.0), ("produce", 10.0), ("produce", 10.0)])
    assert run_log.typical_total_duration(tmp_path, "loop") == 30.0


def test_typical_total_ignores_runs_that_did_not_complete(tmp_path):
    _finish(tmp_path, "wf", "r1", [("scan", 10.0)])
    _finish(tmp_path, "wf", "r2", [("scan", 999.0)], status="aborted")
    assert run_log.typical_total_duration(tmp_path, "wf") == 10.0


def test_typical_total_is_absent_without_history(tmp_path):
    assert run_log.typical_total_duration(tmp_path, "never-run") is None


def test_typical_total_is_absent_when_no_run_measured_anything(tmp_path):
    _finish(tmp_path, "gates-only", "r1", [("gate", None)])
    assert run_log.typical_total_duration(tmp_path, "gates-only") is None


def test_the_engine_records_the_typical_total_on_the_start_manifest(tmp_path):
    """Computed once at run start, like typical_s, so every reader shows the same
    number for the run's whole life instead of recomputing it."""
    from durin.workflow.engine import NodeRunResponse, WorkflowEngine
    from durin.workflow.spec import parse_workflow

    _finish(tmp_path, "wf", "prior", [("a", 12.0), ("b", 8.0)])
    wf = parse_workflow({"name": "wf", "start": "a",
                         "nodes": [{"id": "a", "kind": "work", "next": None}]})
    eng = WorkflowEngine(
        node_runner=lambda req: NodeRunResponse(output="x", session_key=None, messages=[]),
        run_id_factory=lambda: "r-new",
        workspace=str(tmp_path),
    )
    eng.run(wf, "go")

    assert run_log.read_manifest(tmp_path, "wf", "r-new")["typical_total_s"] == 20.0
