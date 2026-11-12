"""The harness itself: it must end a real process at the step asked for, and
what the process leaves behind must be what the engine leaves today."""
import os

from durin.workflow import resume_store, run_log
from durin.workflow.engine import resumable_kind
from durin.workflow.spec import parse_workflow
from tests.workflow.crash_harness import engine, run_to_end, run_until_killed

CHAIN = {"name": "w", "start": "a", "nodes": [
    {"id": "a", "kind": "work", "next": "b"},
    {"id": "b", "kind": "script", "command": "echo b-out", "next": "c"},
    {"id": "c", "kind": "work", "next": None}]}


def test_a_run_ends_normally_in_a_child_process(tmp_path):
    record, marks = run_to_end(tmp_path, CHAIN)
    assert record["status"] == "completed"
    assert marks == {"a": 1, "b": 1, "c": 1}


def test_a_killed_process_leaves_a_running_record_with_its_finished_steps(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b")
    assert dead.record["status"] == "running"
    assert [r["node_id"] for r in dead.record["runs"]] == ["a"]
    assert dead.record["active_node"]["node_id"] == "b"
    assert dead.marks == {"a": 1, "b": 1}


def test_the_sweep_then_calls_it_crashed(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c")
    assert run_log.reconcile_one(dead.workspace, "w", "r1") is True
    assert run_log.read_manifest(dead.workspace, "w", "r1")["status"] == "crashed"


def test_a_process_that_replaced_its_own_image_keeps_its_pid(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b", how="exec")
    assert dead.record["owner"]["pid"] > 0
    os.kill(dead.record["owner"]["pid"], 0)          # still a live process: no exception


def test_a_run_left_by_a_restart_in_place_is_swept(tmp_path, monkeypatch):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b", how="exec")
    owner = dead.record["owner"]
    # The sweep runs in the process that now has that pid: the restarted one.
    monkeypatch.setattr("durin.utils.process_tree._own_pid", lambda: owner["pid"])
    assert run_log.reconcile_one(dead.workspace, "w", "r1") is True


def test_a_killed_process_leaves_the_step_it_was_in_as_the_position(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c")
    assert dead.record["position"]["node"] == "c"
    store = resume_store.store_dir_of(dead.workspace, dead.record)
    node, upstream = resume_store.load_input(store, dead.record["position"]["seq"])
    assert node == "c" and upstream.strip() == "b-out"


def test_a_death_between_the_input_file_and_the_record_keeps_the_earlier_step(tmp_path, monkeypatch):
    # The write that would name "c" fails after c's input file was written: the
    # record still names "b", and the input it points at is still on disk.
    workspace = tmp_path / "ws"
    (workspace / "workflows").mkdir(parents=True)
    real = run_log.update_run
    seen = {}

    def failing(*args, **kwargs):
        position = kwargs.get("position")
        if isinstance(position, dict) and position.get("node") == "c" and "record" not in seen:
            record = run_log.read_manifest(workspace, "w", "r1")
            store = resume_store.store_dir_of(workspace, record)
            seen["record"] = record
            seen["c_input"] = resume_store.load_input(store, position["seq"])
            seen["b_input"] = resume_store.load_input(store, record["position"]["seq"])
            raise OSError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(run_log, "update_run", failing)
    result = engine(workspace).run(parse_workflow(CHAIN), "t")
    assert result.status == "completed"                       # the engine logged and went on
    node, upstream = seen["c_input"]                          # the file was written before the record
    assert node == "c" and upstream.strip() == "b-out"
    assert seen["record"]["position"]["node"] == "b"
    assert seen["b_input"] == ("b", "a-out")


def test_a_process_killed_inside_a_step_leaves_a_cut_attempt_on_record(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c")
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert rec["status"] == "crashed" and rec["failed_node"] == "c"
    assert [(r["node_id"], r.get("cut", False)) for r in rec["runs"]] == [
        ("a", False), ("b", False), ("c", True)]
    assert rec["resume_upstream"].strip() == "b-out"


def test_a_detached_step_running_when_the_process_dies_is_on_record(tmp_path):
    wf = {"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "side"},
        {"id": "side", "kind": "script", "command": "sleep 120", "detached": True, "next": "b"},
        {"id": "b", "kind": "work", "next": None}]}
    dead = run_until_killed(tmp_path, wf, stop_in="b")
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert ("side", True) in [(r["node_id"], r.get("cut", False)) for r in rec["runs"]]
    assert rec["detached_active"] == []


def test_a_process_killed_while_a_detached_step_runs_never_names_it_the_failed_step(tmp_path):
    wf = {"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "side"},
        {"id": "side", "kind": "script", "command": "echo side", "detached": True, "next": "b"},
        {"id": "b", "kind": "work", "next": None}]}
    dead = run_until_killed(tmp_path, wf, stop_in="side")
    assert dead.record["position"] is None or dead.record["position"]["node"] != "side"
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert rec["status"] == "crashed" and rec.get("failed_node") != "side"
    assert ("side", True) in [(r["node_id"], r.get("cut", False)) for r in rec["runs"]]


def test_a_run_someone_stopped_and_that_then_died_is_cancelled_not_resumable(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c")
    # the stop had been asked before the process died
    rec = {**dead.record, "stop_requested": {"at": 1.0, "hard": False}}
    run_log._write_json(run_log._record_path(dead.workspace, "w", "r1"), rec)
    out = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert out["status"] == "cancelled"
    assert resumable_kind(out) is None


def test_a_stop_asked_through_request_stop_outlives_the_process(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c")
    assert run_log.request_stop(dead.workspace, "w", "r1", hard=False) is True
    out = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert out["status"] == "cancelled" and resumable_kind(out) is None
    assert out["stop_requested"]["hard"] is False
    assert not run_log.stop_request_path(dead.workspace, "w", "r1").exists()


def test_a_run_left_by_a_graceful_stop_is_found_dead_and_resumable(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c", how="term")
    out = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert out["status"] == "crashed" and resumable_kind(out) == "dead"


def test_the_reaper_ends_a_group_whose_leader_died_and_its_later_members(tmp_path):
    """A process forked after the snapshot is reachable only through its
    group; when the group's leader is already gone, the reaper must still end
    the group."""
    import signal
    import subprocess
    import time

    from durin.utils.process_tree import process_identity
    from tests.workflow import crash_harness

    proc = subprocess.Popen(["bash", "-c", "read _; sleep 61 & echo $!; read _"],
                            start_new_session=True, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while process_identity(proc.pid)["started"] is None:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        # The snapshot taken while the leader was alone in its group.
        crash_harness._strays.append({**process_identity(proc.pid), "pgid": proc.pid})
        proc.stdin.write("go\n")
        proc.stdin.flush()
        assert int(proc.stdout.readline()) > 0     # the member, forked after the snapshot
        proc.stdin.close()
        proc.wait(timeout=10)                       # the leader is gone

        crash_harness._kill_strays()

        deadline = time.monotonic() + 10
        while _group_has_members(proc.pid):
            assert time.monotonic() < deadline, "the group outlived the reaper"
            time.sleep(0.02)
    finally:
        # Killed as a group only while it still has members: a group's id is
        # never given out while it has members, so this reaches only the test's.
        if _group_has_members(proc.pid):
            os.killpg(proc.pid, signal.SIGKILL)


def _group_has_members(pgid: int) -> bool:
    """Whether group ``pgid`` has a member that has not exited, read with ps."""
    import subprocess

    out = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True,
                         text=True, timeout=10).stdout
    return any(parts[0] == str(pgid) and not parts[1].startswith("Z")
               for parts in (line.split() for line in out.splitlines()) if len(parts) >= 2)
