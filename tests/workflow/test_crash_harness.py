"""The harness itself: it must end a real process at the step asked for, and
what the process leaves behind must be what the engine leaves today."""
import os

from durin.workflow import run_log
from tests.workflow.crash_harness import run_to_end, run_until_killed

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
