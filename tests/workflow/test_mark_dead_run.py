"""One function turns a run whose process is gone into a record a resume can
enter — the record a step failure would have left."""
import json
import threading

from durin.utils.file_lock import cross_process_lock
from durin.utils.process_tree import process_identity
from durin.workflow import resume_store, run_log
from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.result import NodeRun, WorkflowResult
from durin.workflow.spec import parse_workflow

DEAD = {"pid": 2**22 + 4242, "started": "never"}


def _store(tmp_path):
    return resume_store.store_dir(tmp_path, "r1")


def _running(tmp_path, **fields):
    run_log.start_run(tmp_path, "w", "r1", root_session_key=None, started_at=1.0,
                      position={"node": "a", "seq": 1})
    rec = {**run_log.read_manifest(tmp_path, "w", "r1"), "owner": DEAD, **fields}
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    return rec


def test_a_run_whose_owner_is_alive_is_left_alone(tmp_path):
    before = _running(tmp_path, owner=process_identity())
    assert run_log.mark_dead_run(tmp_path, "w", "r1") is None
    assert run_log.read_manifest(tmp_path, "w", "r1") == before


def test_the_step_in_flight_gets_a_cut_row_and_becomes_the_failed_step(tmp_path):
    resume_store.save_input(_store(tmp_path), 2, "b", "what b received")
    _running(tmp_path, position={"node": "b", "seq": 2},
             runs=[{"node_id": "a", "iteration": 1, "status": "ok"}],
             active_node={"node_id": "b", "label": "b", "started_at": 2.0,
                          "iteration": 1, "session_key": "workflow:r1:b:1"})
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec["status"] == "crashed" and rec["failed_node"] == "b"
    assert rec["resume_upstream"] == "what b received"
    assert rec["active_node"] is None and "owner" not in rec
    cut = rec["runs"][-1]
    assert (cut["node_id"], cut["iteration"], cut["status"], cut["cut"]) == ("b", 1, "node_failed", True)
    assert cut["session_key"] == "workflow:r1:b:1" and "process ended" in cut["error"]
    assert cut["pass_number"] is None and cut["budget"] is None
    assert rec == run_log.read_manifest(tmp_path, "w", "r1")


def test_a_death_between_two_steps_leaves_no_cut_row(tmp_path):
    resume_store.save_input(_store(tmp_path), 2, "b", "x")
    _running(tmp_path, position={"node": "b", "seq": 2},
             runs=[{"node_id": "a", "iteration": 1, "status": "ok"}], active_node=None)
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec["failed_node"] == "b" and [r["node_id"] for r in rec["runs"]] == ["a"]


def test_a_detached_step_in_flight_is_recorded_as_cut(tmp_path):
    _running(tmp_path, detached_active=[{"node_id": "note", "iteration": 1, "session_key": None}])
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert [(r["node_id"], r["cut"]) for r in rec["runs"]] == [("note", True)]
    assert rec["detached_active"] == []


def test_a_detached_step_that_is_also_the_step_in_flight_gets_one_cut_row(tmp_path):
    # Between its launch and the write after it, a detached step is both the
    # marked step in flight and a launched detached step.
    entry = {"node_id": "note", "iteration": 1, "session_key": None}
    _running(tmp_path, position={"node": "note", "seq": 1}, detached_active=[entry],
             active_node={**entry, "label": "note", "started_at": 1.0})
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert [(r["node_id"], r["cut"]) for r in rec["runs"]] == [("note", True)]


def test_a_run_someone_asked_to_stop_ends_cancelled(tmp_path):
    _running(tmp_path, stop_requested={"at": 5.0, "hard": False})
    assert run_log.mark_dead_run(tmp_path, "w", "r1")["status"] == "cancelled"


def test_a_cancelled_end_names_no_step_and_drops_the_resume_files(tmp_path):
    resume_store.save_input(_store(tmp_path), 1, "a", "in")
    resume_store.save_source(_store(tmp_path), "a", "out")
    _running(tmp_path, stop_requested={"at": 5.0, "hard": False},
             active_node={"node_id": "a", "label": "a", "started_at": 1.0,
                          "iteration": 1, "session_key": None})
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec["status"] == "cancelled" and rec["position"] is None
    assert not rec.get("failed_node") and not rec.get("resume_upstream")
    assert not rec.get("resume_inputs")
    assert [(r["node_id"], r["cut"]) for r in rec["runs"]] == [("a", True)]
    assert not _store(tmp_path).exists()


def test_a_record_from_before_positions_is_only_marked(tmp_path):
    rec = _running(tmp_path)
    rec.pop("position")
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    out = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert out["status"] == "crashed" and not out.get("failed_node") and out["runs"] == []
    expected = {k: v for k, v in rec.items() if k != "owner"}
    assert out == {**expected, "status": "crashed", "active_node": None}


def test_a_child_run_gets_cut_rows_but_no_failed_step(tmp_path):
    _running(tmp_path, position=None, parent_run_id="caller",
             active_node={"node_id": "a", "label": "a", "started_at": 1.0,
                          "iteration": 1, "session_key": None},
             detached_active=[{"node_id": "note", "iteration": 1, "session_key": None}])
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec["status"] == "crashed" and not rec.get("failed_node")
    assert [(r["node_id"], r["cut"]) for r in rec["runs"]] == [("note", True)]
    assert rec["active_node"] is None


def test_stored_sources_are_merged_over_the_recorded_ones_and_capped(tmp_path):
    long = "y" * (run_log.RESUME_INPUT_MAX_CHARS + 50)
    resume_store.save_input(_store(tmp_path), 1, "a", "z" * (run_log.RESUME_UPSTREAM_MAX_CHARS + 9))
    resume_store.save_source(_store(tmp_path), "src", long)
    _running(tmp_path, resume_inputs={"src": "old", "earlier": "kept"})
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec["resume_inputs"] == {"src": long[:run_log.RESUME_INPUT_MAX_CHARS], "earlier": "kept"}
    assert len(rec["resume_upstream"]) == run_log.RESUME_UPSTREAM_MAX_CHARS


def test_an_input_stored_for_another_step_is_not_used(tmp_path):
    resume_store.save_input(_store(tmp_path), 1, "other", "not a's input")
    _running(tmp_path)
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec["failed_node"] == "a" and rec.get("resume_upstream") is None


def test_an_unreadable_or_malformed_record_is_left_as_it_is(tmp_path):
    path = run_log._record_path(tmp_path, "w", "r1")
    path.write_text("not json", encoding="utf-8")
    assert run_log.mark_dead_run(tmp_path, "w", "r1") is None
    path.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    assert run_log.mark_dead_run(tmp_path, "w", "r1") is None
    for malformed in ({"runs": "not a list"}, {"position": "b"},
                      {"detached_active": [{"iteration": 1}]}):
        path.unlink(missing_ok=True)
        _running(tmp_path, **malformed)
        before = path.read_text(encoding="utf-8")
        assert run_log.mark_dead_run(tmp_path, "w", "r1") is None
        assert path.read_text(encoding="utf-8") == before


def test_an_ownerless_record_is_dead_only_when_old(tmp_path):
    rec = _running(tmp_path)
    rec.pop("owner")
    rec["started_at"] = 1950.0
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    assert run_log.mark_dead_run(tmp_path, "w", "r1", now=2000.0, max_age_s=100.0) is None
    assert run_log.mark_dead_run(tmp_path, "w", "r1", now=2100.0, max_age_s=100.0)["status"] == "crashed"


def test_a_record_whose_lock_is_held_is_skipped(tmp_path, monkeypatch):
    _running(tmp_path)
    monkeypatch.setattr(run_log, "DEAD_RUN_LOCK_TIMEOUT_S", 0.2)
    held, release = threading.Event(), threading.Event()

    def hold():
        with cross_process_lock(run_log.run_lock_target(tmp_path, "w", "r1")):
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    try:
        held.wait(10)
        assert run_log.mark_dead_run(tmp_path, "w", "r1") is None
        assert run_log.reconcile_running(tmp_path, now=10.0, max_age_s=1.0) == 0
        assert run_log.read_manifest(tmp_path, "w", "r1")["status"] == "running"
    finally:
        release.set()
        t.join()
    assert run_log.mark_dead_run(tmp_path, "w", "r1")["status"] == "crashed"


def test_two_finders_convert_it_once(tmp_path):
    resume_store.save_input(_store(tmp_path), 1, "a", None)
    _running(tmp_path, active_node={"node_id": "a", "label": "a", "started_at": 1.0,
                                    "iteration": 1, "session_key": None})
    results = []
    threads = [threading.Thread(target=lambda: results.append(run_log.mark_dead_run(tmp_path, "w", "r1")))
               for _ in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sum(1 for r in results if r is not None) == 1
    assert len(run_log.read_manifest(tmp_path, "w", "r1")["runs"]) == 1


def test_both_sweeps_go_through_it(tmp_path):
    resume_store.save_input(_store(tmp_path), 1, "a", None)
    _running(tmp_path)
    assert run_log.reconcile_running(tmp_path, now=10.0, max_age_s=1.0) == 1
    assert run_log.read_manifest(tmp_path, "w", "r1")["failed_node"] == "a"
    _running(tmp_path, stop_requested={"at": 5.0, "hard": False})
    assert run_log.reconcile_one(tmp_path, "w", "r1") is True
    assert run_log.read_manifest(tmp_path, "w", "r1")["status"] == "cancelled"


# ── the engine keeps detached_active ──

def test_detached_active_survives_rewrites_and_a_finished_run_has_none(tmp_path):
    run_log.start_run(tmp_path, "w", "r1", root_session_key=None, started_at=1.0)
    assert run_log.read_manifest(tmp_path, "w", "r1")["detached_active"] == []
    entry = {"node_id": "note", "iteration": 1, "session_key": None}
    run_log.mark_node_started(tmp_path, "w", "r1", node_id="note", label="note",
                              started_at=2.0, iteration=1, detached_active=[entry])
    run_log.mark_node_started(tmp_path, "w", "r1", node_id="b", label="b", started_at=3.0)
    assert run_log.read_manifest(tmp_path, "w", "r1")["detached_active"] == [entry]
    running = WorkflowResult(status="running", final_output=None, runs=[])
    run_log.update_run(tmp_path, "w", "r1", running)
    assert run_log.read_manifest(tmp_path, "w", "r1")["detached_active"] == [entry]
    run_log.update_run(tmp_path, "w", "r1", running, detached_active=[])
    assert run_log.read_manifest(tmp_path, "w", "r1")["detached_active"] == []
    run_log.mark_node_started(tmp_path, "w", "r1", node_id="note", label="note",
                              started_at=4.0, iteration=2, detached_active=[entry])
    done = WorkflowResult(status="completed", final_output="x", run_id="r1",
                          runs=[NodeRun(node_id="note", iteration=1, output="o")])
    run_log.finalize_run(tmp_path, "w", done, root_session_key=None, started_at=1.0, finished_at=5.0)
    assert run_log.read_manifest(tmp_path, "w", "r1")["detached_active"] == []


def test_the_engine_records_a_detached_step_until_its_row_lands(tmp_path):
    release = threading.Event()
    seen = {}

    def node_runner(req):
        if req.node.id == "side":
            release.wait(10)
        if req.node.id == "b":
            seen["during_b"] = run_log.read_manifest(tmp_path, "w", "r1")
        return NodeRunResponse(output=f"{req.node.id}-out",
                               session_key=f"workflow:r1:{req.node.id}:{req.iteration}")

    def wait_then_release(req):
        out = node_runner(req)
        if req.node.id == "c":
            release.set()
        return out

    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "side"},
        {"id": "side", "kind": "work", "detached": True, "next": "b"},
        {"id": "b", "kind": "work", "next": "c"},
        {"id": "c", "kind": "work", "next": None}]})
    engine = WorkflowEngine(node_runner=wait_then_release, run_id_factory=lambda: "r1",
                            workspace=str(tmp_path))
    result = engine.run(wf, "t")
    assert result.status == "completed"
    during = seen["during_b"]
    assert during["detached_active"] == [
        {"node_id": "side", "iteration": 1, "session_key": "workflow:r1:side:1"}]
    assert "side" not in [r["node_id"] for r in during["runs"]]
    final = run_log.read_manifest(tmp_path, "w", "r1")
    assert final["detached_active"] == []
    side = [r for r in final["runs"] if r["node_id"] == "side"]
    assert len(side) == 1 and side[0]["session_key"] == "workflow:r1:side:1"
