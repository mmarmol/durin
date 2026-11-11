"""What a run's record carries from one walk to the next."""
from durin.workflow import run_log


def _row(node, it, status="ok", **kw):
    return {"node_id": node, "iteration": it, "status": status, **kw}


def _start(tmp_path, **kw):
    return run_log.start_run(tmp_path, "w", "r1", root_session_key=None, started_at=1.0, **kw)


def _write(tmp_path, record):
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), record)


def test_pass_counts_separate_reached_from_completed():
    record = {"runs": [_row("a", 1), _row("b", 1, "node_failed"), _row("b", 2)]}
    assert run_log.pass_counts(record) == {
        "a": {"reached": 1, "completed": 1}, "b": {"reached": 2, "completed": 1}}


def test_pass_counts_add_the_current_walk_to_the_earlier_ones():
    record = {"passes": {"a": {"reached": 2, "completed": 1}},
              "runs": [_row("a", 3), _row("c", 1)]}
    assert run_log.pass_counts(record) == {
        "a": {"reached": 3, "completed": 2}, "c": {"reached": 1, "completed": 1}}


def test_a_record_with_no_carried_fields_counts_its_rows_only():
    assert run_log.pass_counts({"runs": [_row("a", 2)]}) == {"a": {"reached": 2, "completed": 1}}
    assert run_log.pass_counts({}) == {}


def test_a_resume_moves_the_last_walks_rows_to_history_and_keeps_their_counts(tmp_path):
    _start(tmp_path)
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["history"] == [] and rec["passes"] == {}
    _write(tmp_path, {**rec, "status": "aborted", "failed_node": "b",
                      "runs": [_row("a", 1), _row("b", 1, "node_failed")]})
    _start(tmp_path, resumed=True)
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert [(r["node_id"], r["iteration"]) for r in rec["history"]] == [("a", 1), ("b", 1)]
    assert rec["passes"] == {"a": {"reached": 1, "completed": 1},
                             "b": {"reached": 1, "completed": 0}}
    assert rec["runs"] == []


def test_history_and_counts_survive_every_later_write(tmp_path):
    from types import SimpleNamespace
    _start(tmp_path)
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    _write(tmp_path, {**rec, "history": [_row("a", 1)],
                      "passes": {"a": {"reached": 1, "completed": 1}}})
    result = SimpleNamespace(runs=[], run_id="r1", status="completed", final_output="x")
    run_log.update_run(tmp_path, "w", "r1", result)
    assert run_log.read_manifest(tmp_path, "w", "r1")["history"] == [_row("a", 1)]
    run_log.finalize_run(tmp_path, "w", result, root_session_key=None, started_at=1.0, finished_at=2.0)
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["history"] == [_row("a", 1)]
    assert rec["passes"] == {"a": {"reached": 1, "completed": 1}}


def test_history_is_bounded_and_counts_do_not_depend_on_it(tmp_path):
    _start(tmp_path)
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    many = [_row("a", i) for i in range(1, run_log.HISTORY_MAX_ROWS + 1)]
    _write(tmp_path, {**rec, "status": "aborted", "failed_node": "a", "history": many,
                      "passes": {"a": {"reached": len(many), "completed": len(many)}},
                      "runs": [_row("a", len(many) + 1, "node_failed")]})
    _start(tmp_path, resumed=True)
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert len(rec["history"]) == run_log.HISTORY_MAX_ROWS
    assert rec["history"][-1]["iteration"] == len(many) + 1          # newest kept
    assert rec["passes"]["a"] == {"reached": len(many) + 1, "completed": len(many)}


def test_a_resumed_record_from_before_the_counts_credits_attempts_with_no_row():
    # Resumed before ``passes`` existed: ``runs`` holds only the last walk, so
    # every attempt below the highest one ran in a walk the record no longer
    # holds. An attempt with no row counts as completed; one whose rows all
    # failed does not.
    record = {"resumed": True, "runs": [
        _row("a", 3), _row("b", 2, "node_failed"), _row("c", 4, "node_failed"), _row("c", 2)]}
    assert run_log.pass_counts(record) == {
        "a": {"reached": 3, "completed": 3},
        "b": {"reached": 2, "completed": 1},
        "c": {"reached": 4, "completed": 3}}


def test_an_attempt_with_one_row_that_did_not_fail_is_completed_on_an_old_resumed_record():
    record = {"resumed": True, "runs": [
        _row("a", 2, "node_failed", worker_index=0), _row("a", 2, worker_index=1)]}
    assert run_log.pass_counts(record) == {"a": {"reached": 2, "completed": 2}}


def test_a_record_that_carries_counts_is_counted_from_its_rows_even_when_empty():
    # A resumed record written by this version always has ``passes``, even an
    # empty one: its rows are then the only evidence, as on a record never resumed.
    record = {"resumed": True, "passes": {}, "runs": [_row("a", 3)]}
    assert run_log.pass_counts(record) == {"a": {"reached": 3, "completed": 1}}
    assert run_log.pass_counts({"resumed": False, "runs": [_row("a", 3)]}) == {
        "a": {"reached": 3, "completed": 1}}


def test_the_first_resume_after_the_upgrade_carries_the_inferred_counts(tmp_path):
    _start(tmp_path)
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    rec.pop("passes")
    rec.pop("history")
    _write(tmp_path, {**rec, "resumed": True, "status": "aborted", "failed_node": "b",
                      "runs": [_row("a", 2), _row("b", 2, "node_failed")]})
    _start(tmp_path, resumed=True)
    assert run_log.read_manifest(tmp_path, "w", "r1")["passes"] == {
        "a": {"reached": 2, "completed": 2}, "b": {"reached": 2, "completed": 1}}
