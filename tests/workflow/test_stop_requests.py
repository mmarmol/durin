"""A stop someone asked for is kept beside the run's record, so it outlives the
process; the process knows when it is shutting down, which is not a stop."""
from types import SimpleNamespace

from durin.workflow import cancellation, resume_store, run_log
from durin.workflow.result import WorkflowResult

DEAD = {"pid": 2**22 + 4242, "started": "never"}


def _running(tmp_path):
    run_log.start_run(tmp_path, "w", "r1", root_session_key=None, started_at=1.0,
                      position={"node": "a", "seq": 1})


def _sidecar(tmp_path):
    return run_log.stop_request_path(tmp_path, "w", "r1")


def test_a_stop_is_written_on_a_running_record_and_survives_the_next_write(tmp_path):
    _running(tmp_path)
    assert run_log.request_stop(tmp_path, "w", "r1", hard=False) is True
    run_log.update_run(tmp_path, "w", "r1", SimpleNamespace(runs=[], run_id="r1", status="running"))
    run_log.mark_node_started(tmp_path, "w", "r1", node_id="a", label="a", started_at=2.0)
    assert _sidecar(tmp_path).exists()
    stop = run_log.stop_requested(tmp_path, "w", "r1")
    assert stop["hard"] is False and stop["at"] > 0


def test_a_second_stop_can_only_make_it_harder(tmp_path):
    _running(tmp_path)
    run_log.request_stop(tmp_path, "w", "r1", hard=True)
    first = run_log.stop_requested(tmp_path, "w", "r1")
    run_log.request_stop(tmp_path, "w", "r1", hard=False)
    assert run_log.stop_requested(tmp_path, "w", "r1") == first


def test_a_second_stop_upgrades_a_graceful_one_and_keeps_its_time(tmp_path):
    _running(tmp_path)
    run_log.request_stop(tmp_path, "w", "r1", hard=False)
    first = run_log.stop_requested(tmp_path, "w", "r1")
    run_log.request_stop(tmp_path, "w", "r1", hard=True)
    second = run_log.stop_requested(tmp_path, "w", "r1")
    assert second == {"at": first["at"], "hard": True}
    run_log.request_stop(tmp_path, "w", "r1", hard=False)
    assert run_log.stop_requested(tmp_path, "w", "r1") == second


def test_a_run_that_is_not_running_takes_no_stop(tmp_path):
    _running(tmp_path)
    rec = {**run_log.read_manifest(tmp_path, "w", "r1"), "status": "completed"}
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    assert run_log.request_stop(tmp_path, "w", "r1", hard=True) is False
    assert run_log.stop_requested(tmp_path, "w", "r1") is None
    assert "stop_requested" not in run_log.read_manifest(tmp_path, "w", "r1")


def test_a_run_with_no_record_takes_no_stop(tmp_path):
    assert run_log.request_stop(tmp_path, "w", "nope", hard=True) is False
    assert not run_log.stop_request_path(tmp_path, "w", "nope").exists()


def test_a_stop_that_lands_as_the_run_ends_leaves_nothing_behind(tmp_path, monkeypatch):
    # The engine writes the record without the lock. Here its end lands between
    # the stop's first read of the record and the stop's file, so the engine's
    # own removal of that file has already run: the stop reads the record again,
    # takes its file back and says it was not kept.
    _running(tmp_path)
    real_write = run_log._write_stop_request

    def write_after_the_run_ended(path, stop):
        rec = {**run_log.read_manifest(tmp_path, "w", "r1"), "status": "completed"}
        run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
        real_write(path, stop)

    monkeypatch.setattr(run_log, "_write_stop_request", write_after_the_run_ended)
    assert run_log.request_stop(tmp_path, "w", "r1", hard=True) is False
    assert not _sidecar(tmp_path).exists()


def test_the_stop_file_is_never_taken_for_a_run(tmp_path):
    _running(tmp_path)
    run_log.request_stop(tmp_path, "w", "r1", hard=True)
    assert [r["run_id"] for r in run_log.list_runs(tmp_path, "w")] == ["r1"]
    assert [r["run_id"] for r in run_log.read_runs_since(tmp_path, "w")] == ["r1"]
    assert run_log.live_run_ids(tmp_path) == {"r1"}


def test_a_run_that_ends_by_itself_drops_its_stop(tmp_path):
    _running(tmp_path)
    run_log.request_stop(tmp_path, "w", "r1", hard=False)
    done = WorkflowResult(status="cancelled", final_output=None, run_id="r1", runs=[])
    run_log.finalize_run(tmp_path, "w", done, root_session_key=None, started_at=1.0, finished_at=2.0)
    assert not _sidecar(tmp_path).exists()


def test_a_short_circuit_end_drops_the_stop(tmp_path):
    _running(tmp_path)
    run_log.request_stop(tmp_path, "w", "r1", hard=False)
    run_log.finalize_short_circuit(tmp_path, "w", "r1", status="cancelled", final_output=None)
    assert not _sidecar(tmp_path).exists()


def test_pruning_a_record_prunes_its_stop(tmp_path):
    _running(tmp_path)
    _sidecar(tmp_path).write_text('{"at": 1.0, "hard": false}', encoding="utf-8")
    rec = {**run_log.read_manifest(tmp_path, "w", "r1"), "status": "cancelled", "ts": 1.0}
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    run_log.prune_manifests(tmp_path, "w", keep=0)
    assert run_log.read_manifest(tmp_path, "w", "r1") is None
    assert not _sidecar(tmp_path).exists()


def test_a_resume_claim_drops_a_stop_left_from_an_earlier_walk(tmp_path):
    _running(tmp_path)
    rec = {**run_log.read_manifest(tmp_path, "w", "r1"), "status": "aborted", "failed_node": "a"}
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    _sidecar(tmp_path).write_text('{"at": 1.0, "hard": false}', encoding="utf-8")
    run_log.claim_for_resume(tmp_path, "w", "r1")
    assert not _sidecar(tmp_path).exists()


def _dead(tmp_path, **fields):
    _running(tmp_path)
    rec = {**run_log.read_manifest(tmp_path, "w", "r1"), "owner": DEAD, **fields}
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)


def test_a_dead_run_with_a_stop_ends_cancelled_and_its_record_says_so(tmp_path):
    resume_store.save_input(resume_store.store_dir(tmp_path, "r1"), 1, "a", "in")
    _running(tmp_path)
    assert run_log.request_stop(tmp_path, "w", "r1", hard=True) is True
    stop = run_log.stop_requested(tmp_path, "w", "r1")
    rec = {**run_log.read_manifest(tmp_path, "w", "r1"), "owner": DEAD}
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    out = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert out["status"] == "cancelled" and out["position"] is None
    assert out["stop_requested"] == stop
    assert not out.get("failed_node")
    assert not _sidecar(tmp_path).exists()
    assert run_log.read_manifest(tmp_path, "w", "r1") == out


def test_a_stop_written_on_the_record_itself_still_counts(tmp_path):
    _dead(tmp_path, stop_requested={"at": 5.0, "hard": False})
    out = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert out["status"] == "cancelled"
    assert out["stop_requested"] == {"at": 5.0, "hard": False}


def test_an_unreadable_stop_file_is_still_a_stop(tmp_path):
    _dead(tmp_path)
    _sidecar(tmp_path).write_text("not json", encoding="utf-8")
    out = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert out["status"] == "cancelled" and out["stop_requested"]
    assert not _sidecar(tmp_path).exists()


def test_a_dead_run_whose_record_cannot_be_converted_still_honours_the_stop(tmp_path):
    # A record of an odd shape gets only the minimal mark. A stopped run must
    # still end where no resume can enter it, even when it carries the failed
    # step a claimed resume left on it.
    _dead(tmp_path, runs="not a list", failed_node="a")
    _sidecar(tmp_path).write_text('{"at": 3.0, "hard": true}', encoding="utf-8")
    out = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert out["status"] == "cancelled" and out["position"] is None
    assert out["stop_requested"] == {"at": 3.0, "hard": True}
    assert not _sidecar(tmp_path).exists()


def test_the_process_knows_it_is_shutting_down(monkeypatch):
    monkeypatch.setattr(cancellation, "_shutting_down", False, raising=False)
    assert cancellation.shutting_down() is False
    cancellation.begin_shutdown()
    assert cancellation.shutting_down() is True
