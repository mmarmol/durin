"""A script a dead run's process started is ended before its step can run
again: each script process is recorded beside the run's record while it runs,
and the conversion of the dead run ends what is left of it. A group is judged
by its members, never by its leader alone; nothing is signalled without proof
that it is the recorded script; and a resume waits until nothing is left."""
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

# How a refusal ends that names processes a person may have to end.
_CHECK = (" Check that they are that script's before ending them; the run can be resumed "
          "once they have ended.")


def _own(node: str, pids) -> str:
    return (f"the script of its step '{node}' from the earlier process is still running "
            f"(pid {', '.join(str(p) for p in pids)}).{_CHECK}")


def _group(node: str, pids) -> str:
    return (f"processes in the former process group of the script of its step '{node}' are "
            f"still running (pid {', '.join(str(p) for p in pids)}).{_CHECK}")

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


@pytest.fixture
def no_signals(monkeypatch):
    """Every signal other than 0 is recorded as ``(target, sig)`` and dropped,
    so a test naming this process can never signal it, whatever the code under
    test does."""
    sent: list[tuple[int, int]] = []
    real_kill, real_killpg = os.kill, os.killpg

    def kill(pid, sig):
        if not sig:
            return real_kill(pid, sig)
        sent.append((pid, sig))

    def killpg(pgid, sig):
        if not sig:
            return real_killpg(pgid, sig)
        sent.append((pgid, sig))

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", killpg)
    return sent


@pytest.fixture
def unendable():
    """Signals other than 0 reach nothing, as if the processes belonged to
    another user; the waits are shortened. A patch of its own (``.undo()``
    lifts it), undone at this fixture's teardown, before ``groups`` ends the
    test's processes: the shared ``monkeypatch`` is undone only after."""
    patch = pytest.MonkeyPatch()
    real_kill, real_killpg = os.kill, os.killpg
    patch.setattr(os, "kill", lambda pid, sig: real_kill(pid, sig) if not sig else None)
    patch.setattr(os, "killpg", lambda pgid, sig: real_killpg(pgid, sig) if not sig else None)
    patch.setattr(run_log, "DEAD_RUN_SCRIPT_TERM_WAIT_S", 0.2)
    patch.setattr(run_log, "DEAD_RUN_SCRIPT_KILL_WAIT_S", 0.2)
    yield patch
    patch.undo()


# ── a real process killed inside a script step ──

def test_a_script_left_alive_is_ended_and_its_step_runs_once_more(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b", keep_scripts=True)
    [entry] = _recorded(dead.workspace)
    assert entry["node_id"] == "b" and _members(entry["pid"])

    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")

    assert rec["status"] == "crashed" and rec["failed_node"] == "b"
    assert "scripts_left" not in rec
    assert _members(entry["pid"]) == []
    assert run_log.script_records(dead.workspace, "w", "r1") == []
    wf = parse_workflow(CHAIN)
    settled, scripts = run_log.settle_dead_run(dead.workspace, "w", "r1")
    assert scripts is None
    assert resume_refusal(settled, workspace=dead.workspace, workflow=wf) is None
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
    assert all(_members(e["pid"]) for e in entries)

    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")

    assert "scripts_left" not in rec
    assert [_members(e["pid"]) for e in entries] == [[], []]
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
    assert _members(entry["pid"])

    # A resume converts only the run it resumes; the child goes with it.
    run_log.mark_dead_run(dead.workspace, "w", "r1")

    assert _members(entry["pid"]) == []
    child_rec = run_log.read_manifest(dead.workspace, "child", child_run["run_id"])
    assert child_rec["status"] == "crashed"


def test_a_run_that_completes_leaves_no_script_record(tmp_path):
    record, _ = run_to_end(tmp_path, CHAIN)
    assert record["status"] == "completed"
    assert run_log.script_records(tmp_path / "ws", "w", "r1") == []


# ── a group is judged by its members ──

def test_a_member_that_ignores_sigterm_is_killed_when_its_leader_dies_on_it(tmp_path, groups):
    """The leader (bash) dies on SIGTERM; its child ignores SIGTERM and would
    survive a check that looked at the leader alone."""
    folder = _dead_run(tmp_path)
    proc = _start(groups, "(trap '' TERM; sleep 61) & wait")
    _until(lambda: len(_members(proc.pid)) >= 2, "the child to start")
    run_log.record_script(folder, "r1", pid=proc.pid, started=_started(proc.pid), node_id="a")

    t0 = time.monotonic()
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    elapsed = time.monotonic() - t0

    assert _members(proc.pid) == []
    assert "scripts_left" not in rec
    assert run_log.script_records(tmp_path, "w", "r1") == []
    assert elapsed < run_log.DEAD_RUN_SCRIPT_BOUND_S + 1.0


def test_members_of_a_group_whose_leader_is_gone_are_reported_never_signalled(
    tmp_path, groups, signals,
):
    folder = _dead_run(tmp_path)
    proc = _start(groups, "sleep 61 & echo $!; read _", stdin=subprocess.PIPE,
                  stdout=subprocess.PIPE)
    member = int(proc.stdout.readline())
    run_log.record_script(folder, "r1", pid=proc.pid, started=_started(proc.pid), node_id="a")
    proc.stdin.close()
    proc.wait(timeout=_WAIT_S)                       # the leader is gone
    assert _members(proc.pid) == [member]

    rec = run_log.mark_dead_run(tmp_path, "w", "r1")

    assert [s for s in signals if s[0] in (member, proc.pid)] == []
    assert _exists(member)
    [left] = rec["scripts_left"]
    assert left["node_id"] == "a" and [m["pid"] for m in left["members"]] == [member]
    settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal == _group("a", [member])

    os.kill(member, signal.SIGKILL)                  # the person ends it
    _until(lambda: not _exists(member), "the member to end")
    settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal is None
    assert resume_refusal(settled, workspace=tmp_path) is None
    assert resumable_kind(settled) == "dead"


def test_a_pid_that_does_not_lead_its_group_is_never_signalled(tmp_path, groups, no_signals):
    folder = _dead_run(tmp_path)
    proc = _start(groups, "sleep 61 & echo $!; read _", stdin=subprocess.PIPE,
                  stdout=subprocess.PIPE)
    member = int(proc.stdout.readline())
    _until(lambda: _started(member) is not None, "the member's start")
    # A record naming a process of another group, and one naming this process.
    run_log.record_script(folder, "r1", pid=member, started=_started(member), node_id="a")
    run_log.record_script(folder, "r1", pid=os.getpid(), started=_started(os.getpid()),
                          node_id="a")

    rec = run_log.mark_dead_run(tmp_path, "w", "r1")

    assert no_signals == []
    assert _exists(member) and _exists(proc.pid)
    assert sorted(e["pid"] for e in rec["scripts_left"]) == sorted([member, os.getpid()])


def test_a_recorded_pid_that_is_another_process_now_is_forgotten_not_signalled(
    tmp_path, groups, signals,
):
    folder = _dead_run(tmp_path)
    stranger = _start(groups, "sleep 61")
    run_log.record_script(folder, "r1", pid=stranger.pid,
                          started="Thu Jan  1 00:00:00 1970", node_id="a")

    rec = run_log.mark_dead_run(tmp_path, "w", "r1")

    assert signals == []
    assert _exists(stranger.pid)
    assert "scripts_left" not in rec
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


def test_a_final_check_that_fails_leaves_the_script_in_scripts_left(
    tmp_path, groups, monkeypatch,
):
    folder = _dead_run(tmp_path)
    proc = _start(groups, "exec sleep 61")
    run_log.record_script(folder, "r1", pid=proc.pid, started=_started(proc.pid), node_id="a")
    real_table = process_tree._process_table
    calls = []

    def table_then_fail(timeout):
        calls.append(timeout)
        if len(calls) == 1:
            return real_table(timeout)
        raise process_tree.ObservationError("ps failed")

    monkeypatch.setattr(process_tree, "_process_table", table_then_fail)
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    [left] = rec["scripts_left"]
    assert left["unchecked"] is True
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal is not None and "could not be checked" in refusal
    assert str(proc.pid) in refusal

    monkeypatch.setattr(process_tree, "_process_table", real_table)
    proc.wait(timeout=_WAIT_S)          # it died on SIGTERM; reap it
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal is None


_BOUND_S = 1.0


def test_slow_table_reads_while_polling_cannot_stretch_the_wait_past_its_bound(
    groups, signals, monkeypatch,
):
    """SIGTERM goes out to a group whose child ignores it, so the call polls the
    group. Every table read made while polling runs into the full timeout it is
    given (a hung ps) and then fails. The whole call still ends within its
    bound, because each read is only given the time that is left."""
    proc = _start(groups, "(trap '' TERM; sleep 61) & wait")
    _until(lambda: len(_members(proc.pid)) >= 2, "the child to start")
    started = _started(proc.pid)
    real_table = process_tree._process_table
    polled: list[float] = []
    first: list[float] = []

    def table(timeout):
        if not first:
            first.append(timeout)
            return real_table(timeout)          # the first read, at normal speed
        polled.append(timeout)
        time.sleep(max(0.0, timeout))
        raise process_tree.ObservationError("ps timed out")

    monkeypatch.setattr(process_tree, "_process_table", table)
    t0 = time.monotonic()
    [result] = process_tree.end_script_groups(
        [(proc.pid, started)], term_wait_s=2 * _BOUND_S, kill_wait_s=_BOUND_S, bound_s=_BOUND_S)
    elapsed = time.monotonic() - t0

    assert [sig for target, sig in signals if target == proc.pid] == [signal.SIGTERM]
    assert polled, "no table read was made while polling"
    assert elapsed < _BOUND_S + 0.3, f"the call took {elapsed:.2f} s"
    assert all(t <= _BOUND_S for t in polled)
    assert result["ended"] is False and result["unchecked"] is True
    assert _members(proc.pid)                     # the TERM-ignoring child: ``groups`` ends it


# ── a script that cannot be ended ──

def test_a_script_that_cannot_be_ended_keeps_the_run_from_resuming_until_it_is_gone(
    tmp_path, groups, unendable,
):
    folder = _dead_run(tmp_path)
    proc = _start(groups, "exec sleep 61")
    started = _started(proc.pid)
    run_log.record_script(folder, "r1", pid=proc.pid, started=started, node_id="a")

    rec = run_log.mark_dead_run(tmp_path, "w", "r1")

    assert rec["status"] == "crashed"
    [left] = rec["scripts_left"]
    assert (left["pid"], left["node_id"], left["unchecked"]) == (proc.pid, "a", False)
    assert left["members"] == [{"pid": proc.pid, "started": started}]
    assert run_log.read_manifest(tmp_path, "w", "r1") == rec
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal == _own("a", [proc.pid])
    unendable.undo()                    # signals reach it again
    proc.kill()
    proc.wait(timeout=_WAIT_S)
    settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal is None
    assert resumable_kind(settled) == "dead"
    assert resume_refusal(settled, workspace=tmp_path) is None


def test_a_dead_sub_workflow_run_with_a_script_left_refuses_its_callers_resume(
    tmp_path, groups, unendable,
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
    unendable.undo()                    # signals reach it again
    proc.kill()
    proc.wait(timeout=_WAIT_S)
    assert run_log.settle_dead_run(tmp_path, "w", "r1")[1] is None


def test_a_script_record_that_cannot_be_read_refuses_the_resume_and_names_the_file(tmp_path):
    folder = _dead_run(tmp_path)
    deep = folder / "r1.script.4242"
    deep.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec["status"] == "crashed" and "scripts_left" not in rec
    assert deep.exists()
    _settled, refusal = run_log.settle_dead_run(tmp_path, "w", "r1")
    assert refusal == (f"a record of one of its scripts cannot be read ({deep}), so whether "
                       "that script still runs cannot be checked. Remove that file once you "
                       "have made sure nothing of that script is left, then resume.")
    deep.unlink()
    assert run_log.settle_dead_run(tmp_path, "w", "r1")[1] is None


# ── SIGKILL only on a fresh observation; a failed SIGKILL ──

def test_no_sigkill_without_a_fresh_observation_of_the_group(
    tmp_path, groups, signals, monkeypatch,
):
    """After SIGTERM every check of the group fails: nothing proves a member seen
    before is still there, so SIGKILL is not sent and the script is left."""
    folder = _dead_run(tmp_path)
    proc = _start(groups, "(trap '' TERM; sleep 61) & wait")
    _until(lambda: len(_members(proc.pid)) >= 2, "the child to start")
    run_log.record_script(folder, "r1", pid=proc.pid, started=_started(proc.pid), node_id="a")
    monkeypatch.setattr(run_log, "DEAD_RUN_SCRIPT_TERM_WAIT_S", 0.3)
    monkeypatch.setattr(run_log, "DEAD_RUN_SCRIPT_KILL_WAIT_S", 0.3)

    def cannot_tell(_pgid, _members):
        raise process_tree.ObservationError("the group cannot be told")

    monkeypatch.setattr(process_tree, "_group_empty", cannot_tell)
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")

    assert [sig for target, sig in signals if target == proc.pid] == [signal.SIGTERM]
    [left] = rec["scripts_left"]
    assert left["unchecked"] is True


def test_a_sigkill_refused_for_permission_leaves_the_script(tmp_path, groups, monkeypatch):
    folder = _dead_run(tmp_path)
    proc = _start(groups, "(trap '' TERM; sleep 61) & wait")
    _until(lambda: len(_members(proc.pid)) >= 2, "the child to start")
    run_log.record_script(folder, "r1", pid=proc.pid, started=_started(proc.pid), node_id="a")
    monkeypatch.setattr(run_log, "DEAD_RUN_SCRIPT_TERM_WAIT_S", 0.3)
    monkeypatch.setattr(run_log, "DEAD_RUN_SCRIPT_KILL_WAIT_S", 0.3)
    real_killpg = os.killpg
    refused: list[int] = []

    def killpg(pgid, sig):
        if sig == signal.SIGKILL:
            refused.append(pgid)
            raise PermissionError(1, "Operation not permitted")
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", killpg)
    rec = run_log.mark_dead_run(tmp_path, "w", "r1")

    assert refused == [proc.pid]
    [left] = rec["scripts_left"]
    assert left["unchecked"] is False
    assert [m["pid"] for m in left["members"]] == _members(proc.pid)
    assert _members(proc.pid)                     # the TERM-ignoring child is still there


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
    assert refusal == "another caller is acting on it right now. Try again in a moment."
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
