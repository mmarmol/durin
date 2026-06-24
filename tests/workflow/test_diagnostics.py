"""Tests for the pure diagnostic computation."""

from durin.workflow.diagnostics import compute_diagnostics


def _rec(run_id, runs, status="completed"):
    return {"run_id": run_id, "status": status, "runs": runs}


def test_loop_back_counted_per_run_not_per_iteration():
    # node 'a' ran twice in one run (loop-back); 'b' ran once
    recs = [_rec("r1", [
        {"node_id": "a", "iteration": 1, "passed": None},
        {"node_id": "a", "iteration": 2, "passed": None},
        {"node_id": "b", "iteration": 1, "passed": None},
    ])]
    d = compute_diagnostics(recs)
    assert d.loop_backs == {"a": 1}      # one run exhibited the loop-back, not two
    assert "b" not in d.loop_backs


def test_gate_fails_counted_once_per_run():
    recs = [_rec("r1", [
        {"node_id": "g", "iteration": 1, "passed": False},
        {"node_id": "g", "iteration": 2, "passed": True},   # passed on the retry
    ])]
    d = compute_diagnostics(recs)
    assert d.gate_fails == {"g": 1}


def test_candidates_require_recurrence_floor():
    runs = [{"node_id": "a", "iteration": 2, "passed": None}]
    one = compute_diagnostics([_rec("r1", runs)])
    assert one.candidates() == set()                    # a single bad run is noise
    two = compute_diagnostics([_rec("r1", runs), _rec("r2", runs)])
    assert two.candidates() == {"a"}                    # recurs -> candidate


def test_max_visits_aborts_counted():
    d = compute_diagnostics([
        _rec("r1", [{"node_id": "a", "iteration": 3}], status="exhausted"),
        _rec("r2", [{"node_id": "a", "iteration": 1}]),
    ])
    assert d.max_visits_aborts == 1
    assert d.total_runs == 2


def test_parallel_subworkflow_nodes_do_not_choke():
    # ParallelNode/SubworkflowNode produce NodeRuns with passed=None; just don't crash
    d = compute_diagnostics([_rec("r1", [
        {"node_id": "fan", "iteration": 1, "passed": None},
        {"node_id": "sub", "iteration": 1, "passed": None},
    ])])
    assert d.gate_fails == {}
    assert d.candidates() == set()
