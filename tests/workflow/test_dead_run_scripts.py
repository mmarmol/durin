"""A script a dead run's process started is ended before its step can run
again: each script child is recorded beside the run's record while it runs,
and the conversion of the dead run ends the ones still alive."""
import json
import os
import subprocess
import time

import pytest

from durin.utils import process_tree
from durin.workflow import resume_store, run_log
from durin.workflow.engine import (
    NodeRunRequest,
    build_resume_state,
    resumable_kind,
    resume_refusal,
)
from durin.workflow.script_runner import ScriptNodeRunner
from durin.workflow.spec import ScriptNode, parse_workflow
from tests.workflow.crash_harness import engine, marks, run_to_end, run_until_killed

DEAD = {"pid": 2**22 + 4242, "started": "never"}

CHAIN = {"name": "w", "start": "a", "nodes": [
    {"id": "a", "kind": "work", "next": "b"},
    {"id": "b", "kind": "script", "command": "cat; echo ' via b'", "next": "c"},
    {"id": "c", "kind": "work", "next": None}]}


def _alive(entry: dict) -> bool:
    return process_tree.same_live_process(entry["pid"], entry["started"])


def _recorded(workspace, name="w", run_id="r1") -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in run_log.script_records(workspace, name, run_id)]


# ── a real process killed inside a script step ──

def test_a_script_left_alive_is_ended_and_its_step_runs_once_more(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b", keep_scripts=True)
    [entry] = _recorded(dead.workspace)
    assert entry["node_id"] == "b" and _alive(entry)

    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")

    assert rec["status"] == "crashed" and rec["failed_node"] == "b"
    assert "scripts_left" not in rec
    assert not _alive(entry)
    assert run_log.script_records(dead.workspace, "w", "r1") == []
    wf = parse_workflow(CHAIN)
    assert resume_refusal(rec, workspace=dead.workspace, workflow=wf) is None
    done = engine(dead.workspace).run(
        wf, "t", resume=build_resume_state(rec, "", workspace=dead.workspace))
    assert done.status == "completed"
    assert marks(dead.workspace)["b"] == 2          # exactly one new start


def test_both_script_branches_of_a_parallel_step_are_ended(tmp_path):
    from tests.workflow import _crash_driver as driver

    definition = {"name": "w", "start": "pre", "nodes": [
        {"id": "pre", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches": ["left", "right"],
         "reconcile": "union", "next": None},
        {"id": "left", "kind": "script", "command": "sleep 120"},
        {"id": "right", "kind": "script", "command": "echo r"}]}
    dead = run_until_killed(tmp_path, definition, stop_in="right", keep_scripts=True,
                            env={driver.HANG_AFTER_ENV: "left"})
    entries = _recorded(dead.workspace)
    assert sorted(e["node_id"] for e in entries) == ["left", "right"]
    assert all(_alive(e) for e in entries)

    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")

    assert "scripts_left" not in rec
    assert not any(_alive(e) for e in entries)
    assert run_log.script_records(dead.workspace, "w", "r1") == []


def test_a_sub_workflows_script_is_ended_with_its_callers_dead_run(tmp_path):
    parent = {"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "sub"},
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": None}]}
    child = {"name": "child", "start": "x", "nodes": [
        {"id": "x", "kind": "script", "command": "echo x", "next": None}]}
    flows = tmp_path / "ws" / "workflows"
    flows.mkdir(parents=True)
    (flows / "child.json").write_text(json.dumps(child), encoding="utf-8")

    dead = run_until_killed(tmp_path, parent, stop_in="x", keep_scripts=True)
    [child_run] = run_log.list_runs(dead.workspace, "child")
    [entry] = _recorded(dead.workspace, "child", child_run["run_id"])
    assert _alive(entry)

    # A resume converts only the run it resumes; the child goes with it.
    run_log.mark_dead_run(dead.workspace, "w", "r1")

    assert not _alive(entry)
    child_rec = run_log.read_manifest(dead.workspace, "child", child_run["run_id"])
    assert child_rec["status"] == "crashed"


def test_a_run_that_completes_leaves_no_script_record(tmp_path):
    record, _ = run_to_end(tmp_path, CHAIN)
    assert record["status"] == "completed"
    assert run_log.script_records(tmp_path / "ws", "w", "r1") == []


# ── records of a dead run, in this process ──

def _dead_run(tmp_path):
    resume_store.save_input(resume_store.store_dir(tmp_path, "r1"), 1, "a", "in")
    run_log.start_run(tmp_path, "w", "r1", root_session_key=None, started_at=1.0,
                      position={"node": "a", "seq": 1})
    rec = {**run_log.read_manifest(tmp_path, "w", "r1"), "owner": DEAD}
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    return run_log.runs_root(tmp_path) / "w"


def _sleeper():
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    deadline = time.monotonic() + 5
    while process_tree.process_identity(proc.pid)["started"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return proc


def test_a_recorded_pid_that_is_another_process_now_is_forgotten_not_signalled(
    tmp_path, monkeypatch,
):
    folder = _dead_run(tmp_path)
    stranger = _sleeper()
    signalled: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signalled.append(pid))
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append(pid))
    try:
        run_log.record_script(folder, "r1", pid=stranger.pid,
                              started="Thu Jan  1 00:00:00 1970", node_id="a")
        own = os.getpid()
        run_log.record_script(folder, "r1", pid=own,
                              started=process_tree.process_identity(own)["started"],
                              node_id="a")

        rec = run_log.mark_dead_run(tmp_path, "w", "r1")

        assert signalled == []
        assert stranger.poll() is None
        assert "scripts_left" not in rec
        assert run_log.script_records(tmp_path, "w", "r1") == []
    finally:
        monkeypatch.undo()
        stranger.kill()
        stranger.wait()


def test_a_script_that_cannot_be_ended_keeps_the_run_from_resuming_until_it_is_gone(
    tmp_path, monkeypatch,
):
    folder = _dead_run(tmp_path)
    survivor = _sleeper()
    started = process_tree.process_identity(survivor.pid)["started"]
    # Signals do not reach it, as if it belonged to another user.
    monkeypatch.setattr(os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(run_log, "DEAD_RUN_SCRIPT_TERM_WAIT_S", 0.2)
    monkeypatch.setattr(run_log, "DEAD_RUN_SCRIPT_KILL_WAIT_S", 0.2)
    try:
        run_log.record_script(folder, "r1", pid=survivor.pid, started=started, node_id="a")

        rec = run_log.mark_dead_run(tmp_path, "w", "r1")

        assert rec["status"] == "crashed"
        assert rec["scripts_left"] == [{"pid": survivor.pid, "node_id": "a", "started": started}]
        assert run_log.read_manifest(tmp_path, "w", "r1") == rec
        assert resume_refusal(rec, workspace=tmp_path) == (
            f"a script of its step 'a' from the earlier process is still running "
            f"(pid {survivor.pid}). End it, then resume.")
    finally:
        monkeypatch.undo()
        survivor.kill()
        survivor.wait()
    assert resumable_kind(rec) == "dead"
    assert resume_refusal(rec, workspace=tmp_path) is None


# ── the runner keeps the record for exactly as long as the child runs ──

def _request(tmp_path, command, **kw):
    folder = run_log.runs_root(tmp_path) / "w"
    node = ScriptNode(id="s", command=command, **kw)
    return NodeRunRequest(node=node, task="t", upstream_output="", shared_context=[],
                          run_id="r1", iteration=1, root_session_key=None,
                          record_dir=str(folder))


def test_the_runner_records_its_child_while_it_runs_and_forgets_it_after(tmp_path):
    folder = run_log.runs_root(tmp_path) / "w"
    # The runner records the process just after starting it: wait (bounded).
    probe = (f'for _ in $(seq 500); do [ -e "{folder}/r1.script.$$" ] && echo recorded '
             f'&& exit 0; sleep 0.01; done; exit 1')
    resp = ScriptNodeRunner(str(tmp_path))(_request(tmp_path, probe))
    assert resp.output.strip() == "recorded"
    assert run_log.script_records(tmp_path, "w", "r1") == []


def test_a_timed_out_script_is_forgotten(tmp_path):
    with pytest.raises(Exception):
        ScriptNodeRunner(str(tmp_path))(_request(tmp_path, "sleep 30", timeout=1))
    assert run_log.script_records(tmp_path, "w", "r1") == []


def test_a_cancelled_script_is_forgotten(tmp_path):
    req = _request(tmp_path, "sleep 30")
    req.cancel_check = lambda: True
    with pytest.raises(Exception):
        ScriptNodeRunner(str(tmp_path))(req)
    assert run_log.script_records(tmp_path, "w", "r1") == []


def test_a_script_whose_wait_fails_is_ended_and_forgotten(tmp_path, monkeypatch):
    seen: list[int] = []

    def _broken_communicate(self, *a, **kw):
        seen.append(self.pid)
        raise RuntimeError("the wait broke")

    monkeypatch.setattr(subprocess.Popen, "communicate", _broken_communicate)
    with pytest.raises(RuntimeError):
        ScriptNodeRunner(str(tmp_path))(_request(tmp_path, "sleep 30"))
    monkeypatch.undo()
    assert run_log.script_records(tmp_path, "w", "r1") == []
    with pytest.raises(ProcessLookupError):
        os.kill(seen[0], 0)          # ended and reaped: no such process


# ── a run that ends by itself, and pruning ──

def test_a_run_that_ends_or_is_pruned_drops_leftover_records(tmp_path):
    from durin.workflow.result import WorkflowResult

    folder = _dead_run(tmp_path)
    run_log.record_script(folder, "r1", pid=999999, started="x", node_id="a")
    done = WorkflowResult(status="aborted", final_output=None, run_id="r1", runs=[])
    run_log.finalize_run(tmp_path, "w", done, root_session_key=None,
                         started_at=1.0, finished_at=2.0)
    assert run_log.script_records(tmp_path, "w", "r1") == []

    run_log.record_script(folder, "r1", pid=999998, started="x", node_id="a")
    run_log.finalize_short_circuit(tmp_path, "w", "r1", status="cancelled", final_output=None)
    assert run_log.script_records(tmp_path, "w", "r1") == []

    run_log.record_script(folder, "r1", pid=999997, started="x", node_id="a")
    run_log.prune_manifests(tmp_path, "w", keep=0)
    assert run_log.read_manifest(tmp_path, "w", "r1") is None
    assert run_log.script_records(tmp_path, "w", "r1") == []


def test_the_script_records_are_never_taken_for_runs(tmp_path):
    folder = _dead_run(tmp_path)
    run_log.record_script(folder, "r1", pid=999999, started="x", node_id="a")
    assert [r["run_id"] for r in run_log.list_runs(tmp_path, "w")] == ["r1"]
