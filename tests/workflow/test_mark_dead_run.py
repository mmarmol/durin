"""One function turns a run whose process is gone into a record a resume can
enter — the record a step failure would have left."""
import json
import threading
import time

import pytest

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
    # The engine never writes this record (a detached step that started is never
    # the position); one from another build may hold it. The started detached
    # step must not become the step a resume enters.
    resume_store.save_input(_store(tmp_path), 1, "note", "x")
    entry = {"node_id": "note", "iteration": 1, "session_key": None}
    _running(tmp_path, position={"node": "note", "seq": 1}, detached_active=[entry],
             active_node={**entry, "label": "note", "started_at": 1.0})
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert [(r["node_id"], r["cut"]) for r in rec["runs"]] == [("note", True)]
    assert rec["status"] == "crashed" and not rec.get("failed_node")
    assert rec.get("resume_upstream") is None
    assert rec["position"] == {"node": "note", "seq": 1}


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


def test_an_unreadable_record_is_left_as_it_is(tmp_path):
    path = run_log._record_path(tmp_path, "w", "r1")
    path.write_text("not json", encoding="utf-8")
    assert run_log.mark_dead_run(tmp_path, "w", "r1") is None
    assert path.read_text(encoding="utf-8") == "not json"
    path.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    assert run_log.mark_dead_run(tmp_path, "w", "r1") is None
    assert run_log.reconcile_running(tmp_path, now=10.0, max_age_s=1.0) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == ["a", "list"]


def _corrupt_input(tmp_path):
    folder = _store(tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "input-1.json").write_text("{not json", encoding="utf-8")
    return {}


def _non_string_upstream(tmp_path):
    folder = _store(tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "input-1.json").write_text(json.dumps({"node": "a", "upstream": 5}), encoding="utf-8")
    return {}


@pytest.mark.parametrize("make", [
    pytest.param(lambda tmp_path: {"runs": "not a list"}, id="runs-not-a-list"),
    pytest.param(lambda tmp_path: {"position": "b"}, id="position-not-a-dict"),
    pytest.param(lambda tmp_path: {"detached_active": [{"iteration": 1}]}, id="detached-entry-without-id"),
    pytest.param(lambda tmp_path: {"resume_inputs": ["not", "a", "dict"]}, id="resume-inputs-not-a-dict"),
    pytest.param(_corrupt_input, id="stored-input-corrupt"),
    pytest.param(_non_string_upstream, id="stored-upstream-not-text"),
])
def test_a_dead_run_that_cannot_be_converted_is_still_marked_crashed(tmp_path, make):
    fields = make(tmp_path)
    before = _running(tmp_path, active_node={"node_id": "a", "label": "a", "started_at": 1.0,
                                             "iteration": 1, "session_key": None}, **fields)
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec is not None and rec["status"] == "crashed"
    assert rec == run_log.read_manifest(tmp_path, "w", "r1")
    if "runs" in fields or "position" in fields or "detached_active" in fields \
            or "resume_inputs" in fields:
        # The minimal mark: nothing but status, active_node and owner changes.
        expected = {k: v for k, v in before.items() if k != "owner"}
        assert rec == {**expected, "status": "crashed", "active_node": None}
    assert run_log.reconcile_running(tmp_path, now=10.0, max_age_s=1.0) == 0
    assert run_log.reconcile_one(tmp_path, "w", "r1") is False


def test_an_ownerless_record_is_dead_only_when_old(tmp_path):
    rec = _running(tmp_path)
    rec.pop("owner")
    rec["started_at"] = 1950.0
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    assert run_log.mark_dead_run(tmp_path, "w", "r1", now=2000.0, max_age_s=100.0) is None
    assert run_log.mark_dead_run(tmp_path, "w", "r1", now=2100.0, max_age_s=100.0)["status"] == "crashed"


@pytest.mark.parametrize("started_at", [None, 0, -5.0, "x"])
def test_an_ownerless_record_without_a_positive_start_is_never_dead(tmp_path, started_at):
    rec = _running(tmp_path)
    rec.pop("owner")
    rec["started_at"] = started_at
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    assert run_log.reconcile_running(tmp_path, now=10**10, max_age_s=1.0) == 0
    assert run_log.mark_dead_run(tmp_path, "w", "r1", now=10**10, max_age_s=1.0) is None
    assert run_log.read_manifest(tmp_path, "w", "r1")["status"] == "running"


def test_a_record_written_from_nothing_carries_its_writer_and_a_start(tmp_path, monkeypatch):
    # The one read update_run makes fails: it has nothing to carry from.
    monkeypatch.setattr(run_log, "read_manifest", lambda *_a, **_kw: None)
    before = time.time()
    run_log.update_run(tmp_path, "w", "r1",
                       WorkflowResult(status="running", final_output=None, runs=[]),
                       position={"node": "b", "seq": 2})
    monkeypatch.undo()
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["owner"] == process_identity()
    assert before <= rec["started_at"] <= time.time() and rec["ts"] == rec["started_at"]
    assert (rec["status"], rec["workflow"], rec["run_id"]) == ("running", "w", "r1")
    assert rec["position"] == {"node": "b", "seq": 2}
    # Marked incomplete, and the mark survives the walk's later writes.
    assert rec["incomplete"] is True
    run_log.mark_node_started(tmp_path, "w", "r1", node_id="b", label="b", started_at=2.0)
    run_log.update_run(tmp_path, "w", "r1",
                       WorkflowResult(status="running", final_output=None, runs=[]),
                       position={"node": "c", "seq": 3})
    assert run_log.read_manifest(tmp_path, "w", "r1")["incomplete"] is True


def test_a_rewrite_keeps_an_ownerless_record_ownerless(tmp_path):
    # A record that exists without an owner (one a conversion made) is carried
    # as it is: the rewrite never claims a record it found.
    rec = _running(tmp_path)
    rec.pop("owner")
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    run_log.update_run(tmp_path, "w", "r1",
                       WorkflowResult(status="running", final_output=None, runs=[]))
    after = run_log.read_manifest(tmp_path, "w", "r1")
    assert after["owner"] is None and after["started_at"] == 1.0


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


def test_two_finders_convert_it_once(tmp_path, monkeypatch):
    resume_store.save_input(_store(tmp_path), 1, "a", None)
    _running(tmp_path, active_node={"node_id": "a", "label": "a", "started_at": 1.0,
                                    "iteration": 1, "session_key": None})
    record_path = run_log._record_path(tmp_path, "w", "r1")
    real_write = run_log._write_json
    writes = []

    def counting_write(path, data):
        if path == record_path:
            writes.append(data["status"])
        real_write(path, data)

    monkeypatch.setattr(run_log, "_write_json", counting_write)
    finders = 8
    start = threading.Barrier(finders)
    results = []

    def find():
        start.wait(10)
        results.append(run_log.mark_dead_run(tmp_path, "w", "r1"))

    threads = [threading.Thread(target=find) for _ in range(finders)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert writes == ["crashed"]          # one conversion, whatever the timing
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
    running = WorkflowResult(status="running", final_output=None, runs=[])
    run_log.update_run(tmp_path, "w", "r1", running, detached_active=[entry])
    run_log.mark_node_started(tmp_path, "w", "r1", node_id="b", label="b", started_at=3.0)
    assert run_log.read_manifest(tmp_path, "w", "r1")["detached_active"] == [entry]
    run_log.update_run(tmp_path, "w", "r1", running)
    assert run_log.read_manifest(tmp_path, "w", "r1")["detached_active"] == [entry]
    run_log.update_run(tmp_path, "w", "r1", running, detached_active=[])
    assert run_log.read_manifest(tmp_path, "w", "r1")["detached_active"] == []
    run_log.update_run(tmp_path, "w", "r1", running, detached_active=[entry])
    done = WorkflowResult(status="completed", final_output="x", run_id="r1",
                          runs=[NodeRun(node_id="note", iteration=1, output="o")])
    run_log.finalize_run(tmp_path, "w", done, root_session_key=None, started_at=1.0, finished_at=5.0)
    assert run_log.read_manifest(tmp_path, "w", "r1")["detached_active"] == []


_SIDE_CHAIN = {"name": "w", "start": "a", "nodes": [
    {"id": "a", "kind": "work", "next": "side"},
    {"id": "side", "kind": "work", "detached": True, "next": "b"},
    {"id": "b", "kind": "work", "next": "c"},
    {"id": "c", "kind": "work", "next": None}]}

_SIDE_ENTRY = {"node_id": "side", "iteration": 1, "session_key": "workflow:r1:side:1"}


def _reply(req):
    return NodeRunResponse(output=f"{req.node.id}-out",
                           session_key=f"workflow:r1:{req.node.id}:{req.iteration}")


def test_the_engine_records_a_detached_step_until_its_row_lands(tmp_path):
    # `side` finishes while `b` runs, so the write after `b` drains its row:
    # from then on it is in `runs` and no longer in `detached_active`.
    release, side_done = threading.Event(), threading.Event()
    seen = {}

    def node_runner(req):
        if req.node.id == "side":
            release.wait(10)
            side_done.set()
        if req.node.id == "b":
            seen["during_b"] = run_log.read_manifest(tmp_path, "w", "r1")
            release.set()
            side_done.wait(10)
            time.sleep(0.2)   # the tracker's completion callback runs right after `side` returns
        if req.node.id == "c":
            seen["during_c"] = run_log.read_manifest(tmp_path, "w", "r1")
        return _reply(req)

    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                            workspace=str(tmp_path))
    assert engine.run(parse_workflow(_SIDE_CHAIN), "t").status == "completed"
    during_b = seen["during_b"]
    assert during_b["detached_active"] == [_SIDE_ENTRY]
    assert "side" not in [r["node_id"] for r in during_b["runs"]]
    during_c = seen["during_c"]
    assert during_c["detached_active"] == []
    assert [r["session_key"] for r in during_c["runs"] if r["node_id"] == "side"] == [
        "workflow:r1:side:1"]
    final = run_log.read_manifest(tmp_path, "w", "r1")
    assert final["detached_active"] == []
    assert len([r for r in final["runs"] if r["node_id"] == "side"]) == 1


def test_a_started_detached_step_is_never_the_position(tmp_path, monkeypatch):
    # Checked on every record write, and read from inside the detached step and
    # the step after it: the record never holds a step both as its position and
    # as a started detached step, so no death can make it the step to run again.
    record_path = run_log._record_path(tmp_path, "w", "r1")
    real_write = run_log._write_json
    both = []

    def started_and_position(rec):
        position = (rec.get("position") or {}).get("node")
        return position is not None and position in {
            e["node_id"] for e in rec.get("detached_active") or []}

    def checking_write(path, data):
        if path == record_path and started_and_position(data):
            both.append(data)
        real_write(path, data)

    monkeypatch.setattr(run_log, "_write_json", checking_write)
    release = threading.Event()
    seen = {}

    def node_runner(req):
        if req.node.id == "side":
            rec = seen["during_side"] = run_log.read_manifest(tmp_path, "w", "r1")
            seen["next_input"] = resume_store.load_input(
                resume_store.store_dir_of(tmp_path, rec), rec["position"]["seq"])
            release.wait(10)
        if req.node.id == "b":
            seen["during_b"] = run_log.read_manifest(tmp_path, "w", "r1")
            release.set()
        return _reply(req)

    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                            workspace=str(tmp_path))
    assert engine.run(parse_workflow(_SIDE_CHAIN), "t").status == "completed"
    assert both == []
    for rec in (seen["during_side"], seen["during_b"]):
        assert not started_and_position(rec)
        assert _SIDE_ENTRY in rec["detached_active"]
    assert seen["during_side"]["position"]["node"] == "b"
    # The step after the detached one receives the edge text it passes through,
    # stored before the record named it.
    assert seen["next_input"] == ("b", "a-out")


def test_a_detached_step_stays_off_the_position_when_the_next_input_cannot_be_stored(
    tmp_path, monkeypatch,
):
    # The input of b, the step after the detached one, cannot be written: the
    # record then names no step, never the detached step it launched.
    record_path = run_log._record_path(tmp_path, "w", "r1")
    real_write = run_log._write_json
    real_save = resume_store.save_input
    writes = []

    def save(store_dir, seq, node_id, upstream):
        if node_id == "b":
            raise OSError("disk full")
        return real_save(store_dir, seq, node_id, upstream)

    def recording_write(path, data):
        if path == record_path:
            writes.append(json.loads(json.dumps(data)))
        real_write(path, data)

    monkeypatch.setattr(resume_store, "save_input", save)
    monkeypatch.setattr(run_log, "_write_json", recording_write)
    engine = WorkflowEngine(node_runner=_reply, run_id_factory=lambda: "r1",
                            workspace=str(tmp_path))
    assert engine.run(parse_workflow(_SIDE_CHAIN), "t").status == "completed"
    launch = [w for w in writes if _SIDE_ENTRY in (w.get("detached_active") or [])]
    assert launch, "no write recorded the detached step"
    assert all((w.get("position") or {}).get("node") != "side" for w in launch)
    assert launch[0]["position"] is None
