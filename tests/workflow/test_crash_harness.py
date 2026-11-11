"""The harness itself: it must end a real process at the step asked for, and
what the process leaves behind must be what the engine leaves today."""
import os

from durin.workflow import resume_store, run_log
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
