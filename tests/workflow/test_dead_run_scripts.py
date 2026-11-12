"""A step never runs again while a script its dead process started may still
run: each script process is recorded beside the run's record while it runs. A
process that stops or restarts in place ends its own scripts on the way out; a
process killed outright leaves them, and nothing else ever signals them. The
resume of a dead run is refused, naming what is left, until nothing of them
is. A group is judged by its members, never by its leader alone."""
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from durin.utils import process_tree
from durin.utils.file_lock import cross_process_lock
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

_WAIT_S = 10.0

def _own(node: str, pids) -> str:
    return (f"the script of its step '{node}' from the earlier process is still running "
            f"(pid {', '.join(str(p) for p in pids)}). It can be resumed once it has ended.")


def _group(node: str, pids) -> str:
    return (f"processes in the former process group of the script of its step '{node}' are "
            f"still running (pid {', '.join(str(p) for p in pids)}); they may not be that "
            "script's. It can be resumed once they have ended.")

# The real functions, taken at import: the cleanup must reach the processes a
# test started even while that test has signals stubbed out.
_KILLPG = os.killpg


# ── observing processes independently of the code under test ──

def _table() -> list[tuple[int, int, str]]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="],
                         capture_output=True, text=True, timeout=10, check=True)
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def _members(pgid: int) -> list[int]:
    """The pids in group ``pgid`` that have not exited."""
    return sorted(pid for pid, group, stat in _table()
                  if group == pgid and not stat.startswith("Z"))


def _exists(pid: int) -> bool:
    return any(p == pid and not stat.startswith("Z") for p, _g, stat in _table())


def _until(predicate, what: str) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


@pytest.fixture
def groups():
    """Process groups this test starts. Each is killed at the end while it
    still has members (a group's id is never given out while it has members,
    so the kill can only reach what the test started), then its leader is
    reaped."""
    started: list[subprocess.Popen] = []
    yield started
    for proc in started:
        if _members(proc.pid):
            try:
                _KILLPG(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=_WAIT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
    for proc in started:
        _until(lambda p=proc: not _members(p.pid), f"group {proc.pid} to end")


def _start(groups, command: str, *, stdin=None, stdout=None) -> subprocess.Popen:
    """``bash -c command`` in a session of its own, once its start can be read."""
    proc = subprocess.Popen(["bash", "-c", command], start_new_session=True,
                            stdin=stdin, stdout=stdout, text=True)
    groups.append(proc)
    _until(lambda: process_tree.process_identity(proc.pid)["started"] is not None,
           "the process's start")
    return proc


def _started(pid: int) -> str:
    return process_tree.process_identity(pid)["started"]


def _recorded(workspace, name="w", run_id="r1") -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in run_log.script_records(workspace, name, run_id)]


def _dead_run(tmp_path, name="w", run_id="r1", **fields):
    resume_store.save_input(resume_store.store_dir(tmp_path, run_id), 1, "a", "in")
    run_log.start_run(tmp_path, name, run_id, root_session_key=None, started_at=1.0,
                      position={"node": "a", "seq": 1})
    rec = {**run_log.read_manifest(tmp_path, name, run_id), "owner": DEAD, **fields}
    run_log._write_json(run_log._record_path(tmp_path, name, run_id), rec)
    return run_log.runs_root(tmp_path) / name


@pytest.fixture
def signals(monkeypatch):
    """Every signal sent through ``os.kill``/``os.killpg`` as ``(target, sig)``,
    with signal 0 (an existence probe) left out; the signals still go out."""
    sent: list[tuple[int, int]] = []
    real_kill, real_killpg = os.kill, os.killpg

    def kill(pid, sig):
        if sig:
            sent.append((pid, sig))
        return real_kill(pid, sig)

    def killpg(pgid, sig):
        if sig:
            sent.append((pgid, sig))
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", killpg)
    return sent


# ── a real process ended inside a script step ──

def _script_pids(workspace, name="w", run_id="r1") -> list[int]:
    return [e["pid"] for e in _recorded(workspace, name, run_id)]


def _sub_and_child(tmp_path):
    parent = {"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "sub"},
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": "z"},
        {"id": "z", "kind": "work", "next": None}]}
    child = {"name": "child", "start": "x", "nodes": [
        {"id": "x", "kind": "script", "command": "echo x", "next": None}]}
    flows = tmp_path / "ws" / "workflows"
    flows.mkdir(parents=True, exist_ok=True)
    (flows / "child.json").write_text(json.dumps(child), encoding="utf-8")
    return parent


def _parallel(go: Path) -> dict:
    """Two script branches; ``left`` runs until the file ``go`` exists."""
    return {"name": "w", "start": "pre", "nodes": [
        {"id": "pre", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches": ["left", "right"],
         "reconcile": "union", "next": "after"},
        {"id": "left", "kind": "script",
         "command": f'while [ ! -e "{go}" ]; do sleep 0.05; done; echo l'},
        {"id": "right", "kind": "script", "command": "echo r"},
        {"id": "after", "kind": "work", "next": None}]}


def _shape(tmp_path, shape):
    """``(definition, stop_in, env, where)`` of a run ended inside a script:
    ``where`` lists the ``(workflow, run id or None)`` whose script records to
    look at (None: the one child run)."""
    from tests.workflow import _crash_driver as driver

    if shape == "step":
        return CHAIN, "b", None, [("w", "r1")]
    if shape == "sub-workflow":
        return _sub_and_child(tmp_path), "x", None, [("child", None)]
    return (_parallel(tmp_path / "go"), "right", {driver.HANG_AFTER_ENV: "left"},
            [("w", "r1")])


def _scripts_of(dead, where) -> list[dict]:
    out = []
    for name, run_id in where:
        if run_id is None:
            [kid] = run_log.list_runs(dead.workspace, name)
            run_id = kid["run_id"]
        out.extend(_recorded(dead.workspace, name, run_id))
    return out


def _gone(entry) -> bool:
    return (process_tree.process_state(entry["pid"], entry["started"]) == "gone"
            and not _members(entry["pid"]))


def _as_the_restarted_process(monkeypatch, dead):
    """A restart in place keeps the pid: the sweep runs in the process that now
    has it."""
    monkeypatch.setattr(process_tree, "_own_pid", lambda: dead.record["owner"]["pid"])


@pytest.mark.parametrize("how", ["term", "exec"])
@pytest.mark.parametrize("shape", ["step", "sub-workflow", "parallel"])
def test_a_stop_or_a_restart_in_place_leaves_no_script_and_a_resumable_run(
    tmp_path, monkeypatch, shape, how,
):
    definition, stop_in, env, where = _shape(tmp_path, shape)
    go = tmp_path / "go"
    if shape == "sub-workflow":
        _sub_and_child(tmp_path / "plain")
    go.touch()
    uninterrupted, _ = run_to_end(tmp_path / "plain", definition)
    go.unlink()

    dead = run_until_killed(tmp_path, definition, stop_in=stop_in, how=how, env=env,
                            keep_scripts=True)

    scripts = _scripts_of(dead, where)
    assert len(scripts) == (2 if shape == "parallel" else 1)
    for entry in scripts:
        _until(lambda entry=entry: _gone(entry), f"script {entry['pid']} to be gone")
    assert dead.record["status"] == "running"
    if how == "exec":
        _as_the_restarted_process(monkeypatch, dead)
    settled, refusal = run_log.settle_dead_run(dead.workspace, "w", "r1")
    assert refusal is None and resumable_kind(settled) == "dead"
    wf = parse_workflow(definition)
    assert resume_refusal(settled, workspace=dead.workspace, workflow=wf) is None
    go.touch()
    done = engine(dead.workspace).run(
        wf, "t", resume=build_resume_state(settled, "", workspace=dead.workspace))
    assert done.status == "completed"
    assert done.final_output == uninterrupted["final_output"]


GATE = {"name": "w", "start": "a", "nodes": [
    {"id": "a", "kind": "work", "next": "g"},
    {"id": "g", "kind": "script", "command": "exit 0", "on_pass": "p", "on_fail": "f"},
    {"id": "p", "kind": "work", "next": None},
    {"id": "f", "kind": "work", "next": None}]}


@pytest.mark.parametrize("how", ["term", "exec"])
def test_a_gate_whose_script_the_process_ended_takes_no_route(tmp_path, monkeypatch, how):
    """The gate's script is killed by the process's own exit (exit code -9): that
    is never a FAIL verdict, so the run is not walked on down the FAIL route
    and it is left as a crash leaves it, at the gate."""
    dead = run_until_killed(tmp_path, GATE, stop_in="g", how=how, keep_scripts=True)

    [entry] = _recorded(dead.workspace)
    _until(lambda: _gone(entry), "the gate's script to be gone")
    assert dead.record["status"] == "running"
    assert dead.record["position"]["node"] == "g"
    assert [r["node_id"] for r in dead.record["runs"]] == ["a"]
    assert set(dead.marks) == {"a", "g"}
    if how == "exec":
        _as_the_restarted_process(monkeypatch, dead)
    assert run_log.reconcile_one(dead.workspace, "w", "r1") is True
    rec = run_log.read_manifest(dead.workspace, "w", "r1")
    assert rec["status"] == "crashed" and rec["failed_node"] == "g"
    assert [r["node_id"] for r in rec["runs"]] == ["a", "g"] and rec["runs"][-1]["cut"]


def test_a_script_left_by_a_kill_holds_the_resume_until_it_has_ended(tmp_path, signals):
    """A hard kill leaves the script running: nothing ends it, and the resume is
    refused, naming it, until it has ended. The sweep and the resume probe
    processes (signal 0) and send nothing else."""
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b", keep_scripts=True)
    signals.clear()                                  # the harness's own kill of the process
    [entry] = _recorded(dead.workspace)
    assert entry["node_id"] == "b" and _members(entry["pid"])
    wf = parse_workflow(CHAIN)

    settled, refusal = run_log.settle_dead_run(dead.workspace, "w", "r1")

    assert settled["status"] == "crashed" and settled["failed_node"] == "b"
    assert refusal.kind == "script_alive"
    assert refusal.startswith(f"the script of its step 'b' from the earlier process is still "
                              f"running (pid {entry['pid']})")
    assert signals == []
    assert _members(entry["pid"])
    assert [e["pid"] for e in _recorded(dead.workspace)] == [entry["pid"]]

    _KILLPG(entry["pid"], signal.SIGKILL)            # the script ends
    _until(lambda: not _members(entry["pid"]), "the script to end")
    settled, refusal = run_log.settle_dead_run(dead.workspace, "w", "r1")
    assert refusal is None
    assert run_log.script_records(dead.workspace, "w", "r1") == []
    assert resume_refusal(settled, workspace=dead.workspace, workflow=wf) is None
    done = engine(dead.workspace).run(
        wf, "t", resume=build_resume_state(settled, "", workspace=dead.workspace))
    assert done.status == "completed"
    assert marks(dead.workspace)["b"] == 2          # exactly one new start
    assert signals == []        # the test's own end of the script goes around this list


@pytest.mark.parametrize("finder", ["sweep", "tasks"])
def test_a_sweep_and_a_tasks_call_send_no_signal(tmp_path, signals, finder):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b", keep_scripts=True)
    signals.clear()                                  # the harness's own kill of the process
    [entry] = _recorded(dead.workspace)

    if finder == "sweep":
        assert run_log.reconcile_running(
            dead.workspace, now=time.time(), max_age_s=run_log.RECONCILE_AGE_S) == 1
    else:
        assert run_log.reconcile_one(dead.workspace, "w", "r1") is True

    assert run_log.read_manifest(dead.workspace, "w", "r1")["status"] == "crashed"
    assert signals == []
    assert _members(entry["pid"])
    assert _recorded(dead.workspace) == [entry]


def test_a_run_that_completes_leaves_no_script_record(tmp_path):
    record, _ = run_to_end(tmp_path, CHAIN)
    assert record["status"] == "completed"
    assert run_log.script_records(tmp_path / "ws", "w", "r1") == []


# ── a group is judged by its members ──

def test_a_group_whose_leader_is_gone_still_refuses_the_resume(tmp_path, groups, signals):
    folder = _dead_run(tmp_path)
    proc = _start(groups, "sleep 61 & echo $!; read _", stdin=subprocess.PIPE,
                  stdout=subprocess.PIPE)
    member = int(proc.stdout.readline())
    run_log.record_script(folder, "r1", pid=proc.pid, started=_started(proc.pid), node_id="a")
    proc.stdin.close()
    proc.wait(timeout=_WAIT_S)                       # the recorded script is gone
    assert _members(proc.pid) == [member]

    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec["status"] == "crashed"
    settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal == _group("a", [member])
    assert signals == [] and _exists(member)

    _KILLPG(proc.pid, signal.SIGKILL)               # the person ends it
    _until(lambda: not _exists(member), "the member to end")
    settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal is None
    assert resume_refusal(settled, workspace=tmp_path) is None
    assert resumable_kind(settled) == "dead"


def test_a_recorded_pid_that_is_another_process_now_is_forgotten_not_signalled(
    tmp_path, groups, signals,
):
    folder = _dead_run(tmp_path)
    stranger = _start(groups, "sleep 61")
    run_log.record_script(folder, "r1", pid=stranger.pid,
                          started="Thu Jan  1 00:00:00 1970", node_id="a")

    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec["status"] == "crashed"
    assert len(run_log.script_records(tmp_path, "w", "r1")) == 1
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")

    assert refusal is None
    assert signals == []
    assert _exists(stranger.pid)
    assert run_log.script_records(tmp_path, "w", "r1") == []


# ── unknown is never gone ──

def test_an_owner_that_cannot_be_observed_is_not_dead(tmp_path, groups, signals, monkeypatch):
    folder = _dead_run(tmp_path)
    proc = _start(groups, "sleep 61")
    run_log.record_script(folder, "r1", pid=proc.pid, started=_started(proc.pid), node_id="a")

    def cannot_observe(*_a, **_kw):
        raise process_tree.ObservationError("ps timed out")

    real_observe = process_tree._observe
    monkeypatch.setattr(process_tree, "_observe", cannot_observe)
    assert run_log.mark_dead_run(tmp_path, "w", "r1") is None
    monkeypatch.setattr(process_tree, "_observe", real_observe)

    assert run_log.read_manifest(tmp_path, "w", "r1")["status"] == "running"
    assert signals == [] and _exists(proc.pid)
    assert len(run_log.script_records(tmp_path, "w", "r1")) == 1


@pytest.mark.parametrize("started_at", [None, 0, 0.0, "not a time"])
def test_an_ownerless_record_without_a_start_time_is_not_dead(
    tmp_path, groups, signals, started_at,
):
    """A ``running`` record with no ``owner`` and no readable, positive start
    time names no process and no age to judge it by: it may be the record of a
    walk that is still running (written where nothing was left to carry from),
    so no sweep converts it and its script is never signalled."""
    folder = _dead_run(tmp_path)
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    rec.pop("owner")
    rec["started_at"] = started_at
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    proc = _start(groups, "exec sleep 61")
    run_log.record_script(folder, "r1", pid=proc.pid, started=_started(proc.pid), node_id="a")

    now = time.time()
    assert run_log.reconcile_running(tmp_path, now=now, max_age_s=1.0) == 0
    assert run_log.mark_dead_run(tmp_path, "w", "r1", now=now, max_age_s=1.0) is None
    assert run_log.reconcile_one(tmp_path, "w", "r1") is False

    assert run_log.read_manifest(tmp_path, "w", "r1")["status"] == "running"
    assert signals == [] and _exists(proc.pid) and proc.poll() is None
    assert len(run_log.script_records(tmp_path, "w", "r1")) == 1


def test_a_record_rewritten_from_nothing_belongs_to_its_live_writer(tmp_path, groups, signals):
    """The walk's rewrite finds no record to carry from (one read failed, or the
    workflow was renamed while the run was in flight): the record it writes
    names the writer as its owner, so a sweep leaves the live walk and its
    script alone."""
    from durin.workflow.result import WorkflowResult

    run_log.start_run(tmp_path, "old", "r1", root_session_key=None, started_at=time.time(),
                      position={"node": "a", "seq": 1})
    (run_log.runs_root(tmp_path) / "old").rename(run_log.runs_root(tmp_path) / "new")
    run_log.update_run(tmp_path, "old", "r1",
                       WorkflowResult(status="running", final_output=None, runs=[]),
                       position={"node": "b", "seq": 2})
    proc = _start(groups, "exec sleep 61")
    run_log.record_script(run_log.runs_root(tmp_path) / "old", "r1", pid=proc.pid,
                          started=_started(proc.pid), node_id="b")

    assert run_log.reconcile_running(
        tmp_path, now=time.time(), max_age_s=run_log.RECONCILE_AGE_S) == 0

    assert run_log.read_manifest(tmp_path, "old", "r1")["status"] == "running"
    assert signals == [] and _exists(proc.pid) and proc.poll() is None


def test_a_script_that_cannot_be_checked_refuses_the_resume(tmp_path, groups, monkeypatch):
    folder = _dead_run(tmp_path)
    proc = _start(groups, "exec sleep 61")
    run_log.record_script(folder, "r1", pid=proc.pid, started=_started(proc.pid), node_id="a")
    assert run_log.mark_dead_run(tmp_path, "w", "r1")["status"] == "crashed"

    def cannot_read(_timeout):
        raise process_tree.ObservationError("ps failed")

    real_table = process_tree._process_table
    monkeypatch.setattr(process_tree, "_process_table", cannot_read)
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal == _own("a", [proc.pid])           # the script itself is still seen
    proc.kill()
    proc.wait(timeout=_WAIT_S)
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal == (f"whether the script of its step 'a' from the earlier process left "
                       f"anything running could not be checked (pid {proc.pid}). It can be "
                       "resumed once nothing of that script is left.")

    monkeypatch.setattr(process_tree, "_process_table", real_table)
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal is None


# ── a script left running ──

def test_a_dead_sub_workflow_run_with_a_script_left_refuses_its_callers_resume(
    tmp_path, groups,
):
    _dead_run(tmp_path)
    assert run_log.mark_dead_run(tmp_path, "w", "r1")["status"] == "crashed"
    # The child run is found only now, with the caller already crashed.
    child_folder = _dead_run(tmp_path, name="child", run_id="c1", parent_run_id="r1",
                             position=None)
    proc = _start(groups, "exec sleep 61")
    run_log.record_script(child_folder, "c1", pid=proc.pid, started=_started(proc.pid),
                          node_id="x")

    settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")

    assert settled["status"] == "crashed"
    assert run_log.read_manifest(tmp_path, "child", "c1")["status"] == "crashed"
    assert refusal == _own("x", [proc.pid])
    proc.kill()
    proc.wait(timeout=_WAIT_S)
    assert run_log.settle_dead_run(tmp_path, "w", "r1")[1] is None


def test_a_script_record_that_cannot_be_read_refuses_the_resume_and_names_the_file(tmp_path):
    folder = _dead_run(tmp_path)
    deep = folder / "r1.script.4242"
    deep.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec["status"] == "crashed"
    assert deep.exists()
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal == (f"a record of one of its scripts, {deep}, cannot be read, so whether "
                       "that script still runs cannot be checked. It can be resumed once a "
                       "person has made sure nothing of that script is left and has removed "
                       "that file.")
    deep.unlink()
    assert run_log.settle_dead_run(tmp_path, "w", "r1")[1] is None


# ── what else refuses the resume of a dead run ──

def test_a_descendant_still_recorded_running_refuses_the_dead_runs_resume(tmp_path):
    _dead_run(tmp_path)
    assert run_log.mark_dead_run(tmp_path, "w", "r1")["status"] == "crashed"
    # A sub-workflow run whose owner is this live process: it stays running.
    run_log.start_run(tmp_path, "child", "c1", root_session_key=None, started_at=1.0,
                      parent_run_id="r1")

    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")

    assert refusal == ("its sub-workflow run 'c1' of workflow 'child' is still recorded as "
                       "running. It can be resumed once that run has ended or been found dead.")
    assert run_log.read_manifest(tmp_path, "child", "c1")["status"] == "running"


def _owner_alive(pid) -> str:
    return (f"it was marked interrupted while the process that was running it (pid {pid}) "
            "still seems alive. It can be resumed once that process has ended.")


def _crashed_with_owner(tmp_path, owner):
    """A ``crashed`` record that still names an owner: flipped by something
    other than ``mark_dead_run`` (a sweep that only set the status)."""
    _dead_run(tmp_path)
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    rec = {**rec, "owner": owner}
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), rec)
    return rec


def test_the_conversion_drops_the_owner_it_found_gone(tmp_path):
    _dead_run(tmp_path)
    assert "owner" not in run_log.mark_dead_run(tmp_path, "w", "r1")
    assert "owner" not in run_log.read_manifest(tmp_path, "w", "r1")


def test_a_run_converted_after_a_restart_in_place_is_resumed_from_another_process(
    tmp_path, monkeypatch,
):
    """A gateway restarted in place keeps its pid and start, with a new boot id:
    it converts the runs its earlier start left. Another process (the TUI)
    resuming one of them sees that pid alive with the same start, and must
    still be let through: the conversion was the proof the owner was gone."""
    _dead_run(tmp_path, owner={**process_tree.process_identity(), "boot": "an-earlier-start"})
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")      # the restarted gateway converts it
    assert rec is not None and rec["status"] == "crashed" and "owner" not in rec

    # This process now plays another one: the gateway's pid is not its own.
    monkeypatch.setattr(process_tree, "_own_pid", lambda: os.getpid() + 1)
    assert process_tree.process_alive(
        {**process_tree.process_identity(os.getpid()), "boot": "an-earlier-start"}) is True
    settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal is None and resumable_kind(settled) == "dead"
    assert resume_refusal(settled, workspace=tmp_path) is None


def test_a_crashed_run_whose_owner_is_alive_is_not_resumed(tmp_path):
    _crashed_with_owner(tmp_path, process_tree.process_identity())
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal == _owner_alive(os.getpid())


def test_a_crashed_run_whose_owner_cannot_be_observed_is_not_resumed(tmp_path, monkeypatch):
    _crashed_with_owner(tmp_path, {"pid": 4242, "started": "Wed Nov 11 10:00:00 2026"})

    def cannot_observe(*_a, **_kw):
        raise process_tree.ObservationError("ps timed out")

    monkeypatch.setattr(process_tree, "_observe", cannot_observe)
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal == _owner_alive(4242)


@pytest.mark.parametrize("owner", [
    DEAD,                                                              # gone
    {"pid": os.getpid(), "started": "Thu Jan  1 00:00:00 1970"},       # pid now another's
])
def test_a_crashed_run_whose_owner_is_gone_is_resumed(tmp_path, owner):
    _crashed_with_owner(tmp_path, owner)
    settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal is None and resumable_kind(settled) == "dead"


_TAKEN = "it was cancelled, or already resumed, before this reached it."


@pytest.mark.parametrize("live", [True, False], ids=["live-script", "ended-script"])
def test_a_check_that_finds_the_run_claimed_removes_nothing_and_says_so(
    tmp_path, groups, monkeypatch, live,
):
    """Two resumes of one dead run at once: the second read the record before
    the first claimed it. When the second checks the scripts, the first's new
    walk has recorded a script of its own (still running, or just ended and not
    yet forgotten). The second must neither describe it as left by the earlier
    process nor remove its record."""
    folder = _dead_run(tmp_path)
    assert run_log.mark_dead_run(tmp_path, "w", "r1")["status"] == "crashed"
    if live:
        proc = _start(groups, "exec sleep 61")
        pid, started = proc.pid, _started(proc.pid)
    else:
        pid, started = DEAD["pid"], DEAD["started"]
    new_walk: list[Path] = []
    real_mark_children = run_log._mark_dead_children

    def claimed_meanwhile(*args, **kwargs):
        real_mark_children(*args, **kwargs)
        # The first resume claims the run under its lock; its walk starts a script.
        with cross_process_lock(run_log.run_lock_target(tmp_path, "w", "r1")):
            run_log.claim_for_resume(tmp_path, "w", "r1")
        new_walk.append(run_log.record_script(folder, "r1", pid=pid, started=started,
                                              node_id="a"))

    monkeypatch.setattr(run_log, "_mark_dead_children", claimed_meanwhile)
    settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")

    assert settled["status"] == "crashed"                 # what it read first
    assert refusal == _TAKEN
    [path] = new_walk
    assert path.exists()
    claimed = run_log.read_manifest(tmp_path, "w", "r1")
    assert claimed["status"] == "running" and claimed["owner"]["pid"] == os.getpid()


_BUSY = "another caller is acting on it right now. Try again in a moment."


def _between_observation_and_decision(monkeypatch, name, change):
    """Run ``change()`` right after the scripts of run ``name`` are observed
    (holding no lock) and before the decision taken under its lock."""
    real = run_log._script_checks

    def observe_then_change(workspace, run_name, rec):
        checks = real(workspace, run_name, rec)
        if run_name == name:
            change()
        return checks

    monkeypatch.setattr(run_log, "_script_checks", observe_then_change)


def test_a_script_record_replaced_before_the_decision_is_kept_and_refuses(
    tmp_path, monkeypatch,
):
    """Its process was proven gone; then, before the decision, the file names a
    new process (the same pid, another start). It is not removed, and the
    resume is refused until it is checked again."""
    folder = _dead_run(tmp_path)
    assert run_log.mark_dead_run(tmp_path, "w", "r1")["status"] == "crashed"
    path = run_log.record_script(folder, "r1", pid=DEAD["pid"], started=DEAD["started"],
                                 node_id="a")
    newer = {"pid": DEAD["pid"], "started": "a later start", "node_id": "a"}
    _between_observation_and_decision(monkeypatch, "w", lambda: run_log.record_script(
        folder, "r1", pid=newer["pid"], started=newer["started"], node_id="a"))

    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")

    assert refusal == _BUSY
    assert json.loads(path.read_text(encoding="utf-8")) == newer


def test_a_sub_workflow_run_whose_record_changes_before_the_decision_refuses_busy(
    tmp_path, monkeypatch,
):
    _dead_run(tmp_path)
    assert run_log.mark_dead_run(tmp_path, "w", "r1")["status"] == "crashed"
    child_folder = _dead_run(tmp_path, name="child", run_id="c1", parent_run_id="r1",
                             position=None)
    assert run_log.mark_dead_run(tmp_path, "child", "c1")["status"] == "crashed"
    path = run_log.record_script(child_folder, "c1", pid=DEAD["pid"], started=DEAD["started"],
                                 node_id="x")

    def rewrite_the_child():
        changed = {**run_log.read_manifest(tmp_path, "child", "c1"), "ts": 99.0}
        run_log._write_json(run_log._record_path(tmp_path, "child", "c1"), changed)

    _between_observation_and_decision(monkeypatch, "child", rewrite_the_child)

    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")

    assert refusal == _BUSY
    assert path.exists()


def test_a_run_resumed_and_found_dead_again_before_the_decision_is_refused(
    tmp_path, monkeypatch,
):
    """Between the observation and the decision, another resume claims the run,
    its walk records a script, its process dies and the run is found dead
    again: ``crashed`` once more, but another record. Nothing of the new walk
    is removed, even a record the observation had proven over."""
    folder = _dead_run(tmp_path)
    assert run_log.mark_dead_run(tmp_path, "w", "r1")["status"] == "crashed"
    path = run_log.record_script(folder, "r1", pid=DEAD["pid"], started=DEAD["started"],
                                 node_id="a")

    def resumed_and_dead_again():
        with cross_process_lock(run_log.run_lock_target(tmp_path, "w", "r1")):
            claimed = run_log.claim_for_resume(tmp_path, "w", "r1")
        run_log.record_script(folder, "r1", pid=DEAD["pid"], started=DEAD["started"],
                              node_id="a")
        again = {k: v for k, v in claimed.items() if k != "owner"}
        again.update(status="crashed", ts=2.0, finished_at=2.0,
                     runs=[run_log._cut_row({"node_id": "a", "iteration": 1})])
        run_log._write_json(run_log._record_path(tmp_path, "w", "r1"), again)

    _between_observation_and_decision(monkeypatch, "w", resumed_and_dead_again)

    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")

    assert refusal == _TAKEN
    assert path.exists()
    assert run_log.read_manifest(tmp_path, "w", "r1")["runs"][-1]["cut"] is True


def test_a_run_whose_record_is_removed_before_the_decision_says_so(tmp_path, monkeypatch):
    folder = _dead_run(tmp_path)
    assert run_log.mark_dead_run(tmp_path, "w", "r1")["status"] == "crashed"
    run_log.record_script(folder, "r1", pid=DEAD["pid"], started=DEAD["started"], node_id="a")
    _between_observation_and_decision(
        monkeypatch, "w", lambda: run_log._record_path(tmp_path, "w", "r1").unlink())

    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")

    assert refusal == "its record no longer exists."


def test_a_check_whose_run_lock_stays_busy_says_the_run_is_busy(tmp_path, monkeypatch):
    _dead_run(tmp_path)
    assert run_log.mark_dead_run(tmp_path, "w", "r1")["status"] == "crashed"
    monkeypatch.setattr(run_log, "DEAD_RUN_LOCK_TIMEOUT_S", 0.3)
    held, release = threading.Event(), threading.Event()

    def hold_the_lock():
        with cross_process_lock(run_log.run_lock_target(tmp_path, "w", "r1")):
            held.set()
            release.wait(_WAIT_S)

    holder = threading.Thread(target=hold_the_lock)
    holder.start()
    try:
        assert held.wait(_WAIT_S)
        _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    finally:
        release.set()
        holder.join(_WAIT_S)
    assert not holder.is_alive()
    assert refusal == _BUSY
    assert run_log.settle_dead_run(tmp_path, "w", "r1")[1] is None


@pytest.mark.parametrize("fields", [
    {"status": "needs_input", "needs_input_node": "a"},
    {"status": "aborted", "failed_node": "a"},
])
def test_a_paused_or_failed_run_is_not_held_by_a_live_owner_or_descendant(tmp_path, fields):
    _dead_run(tmp_path, owner=process_tree.process_identity(), **fields)
    run_log.start_run(tmp_path, "child", "c1", root_session_key=None, started_at=1.0,
                      parent_run_id="r1")
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal is None


# ── the start identity ──

def _proc_line(comm: str, *, state="S", pgid=77, ticks=12345) -> str:
    rest = [state, "1", str(pgid)] + ["0"] * 16 + [str(ticks), "0", "0"]
    return f"4242 ({comm}) " + " ".join(rest) + "\n"


@pytest.fixture
def proc_readers(monkeypatch):
    """/proc as the test says it is: ``state["boot"]`` and ``state["line"]``."""
    state = {"boot": "boot-a", "line": _proc_line("sleep")}

    def boot():
        return state["boot"]

    def stat(pid):
        line = state["line"]
        if isinstance(line, Exception):
            raise line
        return line

    monkeypatch.setattr(process_tree, "_read_boot_id", boot)
    monkeypatch.setattr(process_tree, "_read_proc_stat", stat)
    return state


def test_the_same_boot_and_start_ticks_are_the_same_process(proc_readers):
    seen = process_tree._observe(4242)
    assert seen.started == "proc:boot-a:12345" and seen.pgid == 77 and not seen.zombie
    assert process_tree._same_start(seen, "proc:boot-a:12345") is True
    assert process_tree._same_start(seen, "proc:boot-b:12345") is False
    assert process_tree._same_start(seen, "proc:boot-a:12346") is False


def test_an_identity_recorded_the_old_way_is_compared_the_old_way(proc_readers, monkeypatch):
    seen = process_tree._observe(4242)
    monkeypatch.setattr(process_tree, "_lstart_of", lambda pid, timeout: "Wed Nov 11 10:00:00 2026")
    assert process_tree._same_start(seen, "Wed Nov 11 10:00:00 2026") is True
    assert process_tree._same_start(seen, "Tue Nov 10 10:00:00 2026") is False


def test_a_command_name_with_spaces_and_parentheses_parses(proc_readers):
    proc_readers["line"] = _proc_line("a) (b c", state="Z", pgid=99, ticks=7)
    seen = process_tree._observe(4242)
    assert (seen.pgid, seen.zombie, seen.started) == (99, True, "proc:boot-a:7")


def test_proc_reads_decide_alive_gone_or_unknown(proc_readers, monkeypatch):
    owner = {"pid": 4242, "started": "proc:boot-a:12345"}
    assert process_tree.process_alive(owner) is True
    assert process_tree.process_alive({**owner, "started": "proc:boot-a:1"}) is False
    proc_readers["line"] = FileNotFoundError()
    monkeypatch.setattr(process_tree, "_no_such_process", lambda pid: True)
    assert process_tree.process_alive(owner) is False
    proc_readers["line"] = OSError(5, "I/O error")
    assert process_tree.process_alive(owner) is True          # unknown is not dead
    assert process_tree.process_state(4242, owner["started"]) == "unknown"


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


def _sleeping_script(tmp_path) -> tuple[str, Path]:
    """A command that writes its own pid to a file, then sleeps: the test finds
    the process through the file, whatever the runner did with it."""
    pid_file = tmp_path / "script.pid"
    return f'echo $$ > "{pid_file}"; exec sleep 30', pid_file


def _end_script_of(pid_file) -> None:
    if pid_file.exists():
        _end_group_left_by_the_runner([int(pid_file.read_text().strip())])


def test_a_timed_out_script_is_forgotten(tmp_path):
    command, pid_file = _sleeping_script(tmp_path)
    try:
        with pytest.raises(Exception):
            ScriptNodeRunner(str(tmp_path))(_request(tmp_path, command, timeout=1))
        assert run_log.script_records(tmp_path, "w", "r1") == []
    finally:
        _end_script_of(pid_file)


def test_a_cancelled_script_is_forgotten(tmp_path):
    command, pid_file = _sleeping_script(tmp_path)
    req = _request(tmp_path, command)
    req.cancel_check = lambda: True
    try:
        with pytest.raises(Exception):
            ScriptNodeRunner(str(tmp_path))(req)
        assert run_log.script_records(tmp_path, "w", "r1") == []
    finally:
        _end_script_of(pid_file)


def _end_group_left_by_the_runner(pids: list[int]) -> None:
    """End the group of a script the runner started, if the runner left it (the
    defect these tests catch). The runner starts each script in a session of
    its own, and a group's id is never given out while it has members."""
    for pid in pids:
        if _members(pid):
            _KILLPG(pid, signal.SIGKILL)
            _until(lambda p=pid: not _members(p), f"group {pid} to end")


def test_a_script_whose_wait_fails_is_ended_and_forgotten(tmp_path, monkeypatch):
    seen: list[int] = []
    real_communicate = subprocess.Popen.communicate

    def _broken_communicate(self, *a, **kw):
        # Only the script's own wait breaks; a ps the runner runs is untouched.
        if self.args[:1] != ["bash"]:
            return real_communicate(self, *a, **kw)
        seen.append(self.pid)
        raise RuntimeError("the wait broke")

    monkeypatch.setattr(subprocess.Popen, "communicate", _broken_communicate)
    try:
        with pytest.raises(RuntimeError):
            ScriptNodeRunner(str(tmp_path))(_request(tmp_path, "sleep 30"))
        monkeypatch.setattr(subprocess.Popen, "communicate", real_communicate)
        assert len(seen) == 1
        assert run_log.script_records(tmp_path, "w", "r1") == []
        with pytest.raises(ProcessLookupError):
            os.kill(seen[0], 0)          # ended and reaped: no such process
    finally:
        _end_group_left_by_the_runner(seen)


def test_a_record_failure_before_the_wait_still_ends_the_script(tmp_path, monkeypatch):
    seen: list[int] = []

    def record_fails(record_dir, run_id, *, pid, started, node_id):
        seen.append(pid)
        raise RuntimeError("the record could not be built")

    monkeypatch.setattr(run_log, "record_script", record_fails)
    try:
        with pytest.raises(RuntimeError):
            ScriptNodeRunner(str(tmp_path))(_request(tmp_path, "sleep 30"))
        with pytest.raises(ProcessLookupError):
            os.kill(seen[0], 0)
    finally:
        _end_group_left_by_the_runner(seen)


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


def test_on_linux_a_missing_stat_file_is_gone_only_when_signal_0_agrees(
    proc_readers, monkeypatch,
):
    proc_readers["line"] = FileNotFoundError()
    monkeypatch.setattr(process_tree, "_no_such_process", lambda pid: True)
    assert process_tree._observe(4242) is None
    monkeypatch.setattr(process_tree, "_no_such_process", lambda pid: False)
    with pytest.raises(process_tree.ObservationError):
        process_tree._observe(4242)


def _proc_table(monkeypatch, reads: dict):
    """The /proc table as the test says it is: pid -> a stat line or an error."""
    monkeypatch.setattr(process_tree, "_read_boot_id", lambda: "boot-a")
    monkeypatch.setattr(process_tree, "_list_proc_pids", lambda: sorted(reads))

    def stat(pid):
        read = reads[pid]
        if isinstance(read, Exception):
            raise read
        return read

    monkeypatch.setattr(process_tree, "_read_proc_stat", stat)
    return process_tree._process_table(1.0)


def test_a_process_that_exits_while_the_table_is_read_counts_as_gone(monkeypatch):
    table = _proc_table(monkeypatch, {1: _proc_line("a", pgid=1), 2: FileNotFoundError(),
                                      3: ProcessLookupError()})
    assert [m.pid for m in table] == [1]


def test_a_process_of_another_user_is_skipped(monkeypatch):
    table = _proc_table(monkeypatch, {1: _proc_line("a", pgid=1),
                                      2: PermissionError(13, "Permission denied")})
    assert [m.pid for m in table] == [1]


def test_any_other_read_error_makes_the_table_unknown(monkeypatch):
    with pytest.raises(process_tree.ObservationError):
        _proc_table(monkeypatch, {1: _proc_line("a", pgid=1), 2: OSError(5, "I/O error")})
