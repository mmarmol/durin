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


def test_script_failures_counted_per_run_not_per_row():
    # two node_failed rows for 's' land in the same run -> counts as 1 run, not 2
    recs = [_rec("r1", [
        {"node_id": "s", "iteration": 1, "status": "node_failed", "error": "boom1"},
        {"node_id": "s", "iteration": 2, "status": "node_failed", "error": "boom2"},
    ])]
    d = compute_diagnostics(recs)
    assert d.script_failures == {"s": 1}


def test_script_failures_ignore_ok_rows():
    recs = [_rec("r1", [
        {"node_id": "s", "iteration": 1, "status": "ok", "error": None},
    ])]
    d = compute_diagnostics(recs)
    assert d.script_failures == {}


def test_failure_samples_deduped_capped_newest_first():
    # records arrive oldest -> newest; samples must read newest run first,
    # skip exact duplicates, and cap at 3 distinct errors
    recs = [
        _rec("r0", [{"node_id": "s", "iteration": 1, "status": "node_failed", "error": "err_w"}]),
        _rec("r1", [{"node_id": "s", "iteration": 1, "status": "node_failed", "error": "err_x"}]),
        _rec("r2", [{"node_id": "s", "iteration": 1, "status": "node_failed", "error": "err_y"}]),
        _rec("r3", [{"node_id": "s", "iteration": 1, "status": "node_failed", "error": "err_y"}]),
        _rec("r4", [{"node_id": "s", "iteration": 1, "status": "node_failed", "error": "err_z"}]),
    ]
    d = compute_diagnostics(recs)
    # newest run (r4) first; r3's "err_y" is a duplicate of r2's and is skipped;
    # r0's "err_w" is dropped once the cap of 3 distinct errors is reached
    assert d.failure_samples == {"s": ["err_z", "err_y", "err_x"]}


def test_failure_samples_skip_empty_error_strings():
    recs = [_rec("r1", [
        {"node_id": "s", "iteration": 1, "status": "node_failed", "error": ""},
        {"node_id": "s", "iteration": 2, "status": "node_failed", "error": None},
    ])]
    d = compute_diagnostics(recs)
    assert d.script_failures == {"s": 1}   # still counted as a failing run
    assert d.failure_samples == {}         # but no usable sample text


def test_candidates_include_script_failures_at_floor():
    recs = [{"node_id": "s", "iteration": 1, "status": "node_failed", "error": "boom"}]
    one = compute_diagnostics([_rec("r1", recs)])
    assert one.candidates() == set()
    two = compute_diagnostics([_rec("r1", recs), _rec("r2", recs)])
    assert two.candidates() == {"s"}


def test_script_failures_do_not_affect_loop_backs_or_gate_fails():
    recs = [_rec("r1", [
        {"node_id": "a", "iteration": 1, "passed": None},
        {"node_id": "a", "iteration": 2, "passed": None},
        {"node_id": "g", "iteration": 1, "passed": False},
        {"node_id": "s", "iteration": 1, "status": "node_failed", "error": "boom"},
    ])]
    d = compute_diagnostics(recs)
    assert d.loop_backs == {"a": 1}
    assert d.gate_fails == {"g": 1}
    assert d.script_failures == {"s": 1}
