"""A process ends its own scripts at the point it ends or replaces itself
(``script_runner.end_scripts_for_exit``), and a script ended that way never
becomes an outcome: no failed step, no ``FAIL`` verdict of a gate, no route
taken, no step after it started, and no script started once the pass ran.

These tests run the real pass inside the test process. The runner's wait for
the process to end is the one thing replaced (``exit_pass``): the process does
not end here, so the test releases the threads it held once it has looked."""
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from durin.utils import process_tree
from durin.workflow import run_log, script_runner
from durin.workflow.engine import NodeRunRequest
from durin.workflow.script_runner import ScriptNodeRunner
from durin.workflow.spec import ScriptNode, parse_workflow
from tests.workflow.crash_harness import engine, marks

_WAIT_S = 10.0
# The real function, taken at import: the cleanup must reach a group a test
# started even while that test records the calls to ``os.killpg``.
_KILLPG = os.killpg


class _Released(BaseException):
    """Ends a thread the test held in the wait for the process to end. Not an
    ``Exception``: nothing on the way out may take it for a step's failure."""


class _ExitPass:
    """What ``exit_pass`` gives a test: the threads it holds, and how many of
    them have reached the wait for the process to end."""

    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._held = 0

    def wait_for_exit(self) -> None:
        with self._lock:
            self._held += 1
        self.release.wait()
        raise _Released

    def held(self) -> int:
        with self._lock:
            return self._held

    def start(self, target, *args) -> threading.Thread:
        def run() -> None:
            try:
                target(*args)
            except _Released:
                pass
        thread = threading.Thread(target=run, name="exit-pass-test")
        self.threads.append(thread)
        thread.start()
        return thread


@pytest.fixture
def exit_pass(monkeypatch):
    """The runner waits on the test instead of until the process ends. At
    teardown every thread the test started is released and joined, and the
    process-ending state is cleared, before the suite-wide check that no test
    leaves it set."""
    state = _ExitPass()
    monkeypatch.setattr(script_runner, "_wait_for_exit", state.wait_for_exit)
    yield state
    state.release.set()
    for thread in state.threads:
        thread.join(_WAIT_S)
    assert not any(t.is_alive() for t in state.threads), "a held thread did not end"
    script_runner._exiting = False


def _until(predicate, what: str) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def _stat(pid: int) -> str:
    out = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                         capture_output=True, text=True, timeout=10)
    return out.stdout.strip()


def _pid_in(path: Path) -> int:
    _until(lambda: path.is_file() and path.read_text().strip(), f"{path.name}")
    return int(path.read_text().strip())


def _group_members(pgid: int) -> list[int]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="],
                         capture_output=True, text=True, timeout=10).stdout
    return [int(p) for p, g, s in (line.split()[:3] for line in out.splitlines()
                                   if len(line.split()) >= 3)
            if int(g) == pgid and not s.startswith("Z")]


@pytest.fixture
def groups():
    """Groups the test's scripts lead, ended at teardown while they still have
    members (a group's id is never given out while it has members)."""
    leaders: list[int] = []
    yield leaders
    for pgid in leaders:
        if _group_members(pgid):
            try:
                _KILLPG(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _request(tmp_path, command: str) -> NodeRunRequest:
    folder = run_log.runs_root(tmp_path) / "w"
    return NodeRunRequest(node=ScriptNode(id="s", command=command), task="t",
                          upstream_output="", shared_context=[], run_id="r1", iteration=1,
                          root_session_key=None, record_dir=str(folder))


@pytest.fixture
def killpg_calls(monkeypatch):
    """Every ``os.killpg`` call other than signal 0, as ``(pgid, sig)``; the
    signals still go out."""
    calls: list[tuple[int, int]] = []
    real = os.killpg

    def killpg(pgid, sig):
        if sig:
            calls.append((pgid, sig))
        return real(pgid, sig)

    monkeypatch.setattr(os, "killpg", killpg)
    return calls


def test_end_scripts_for_exit_ends_a_running_script_and_its_children(
        tmp_path, exit_pass, groups):
    leader_file, child_file = tmp_path / "leader", tmp_path / "child"
    command = (f'sleep 61 & echo $! > "{child_file}"; echo $$ > "{leader_file}"; wait')
    exit_pass.start(ScriptNodeRunner(str(tmp_path)), _request(tmp_path, command))
    leader, child = _pid_in(leader_file), _pid_in(child_file)
    groups.append(leader)
    started = {pid: process_tree.process_identity(pid)["started"] for pid in (leader, child)}
    assert all(started.values())

    assert script_runner.end_scripts_for_exit() == 1

    for pid in (leader, child):
        _until(lambda pid=pid: process_tree.process_state(pid, started[pid]) == "gone",
               f"pid {pid} to be gone")
    _until(lambda: exit_pass.held() == 1, "the runner to wait for the process end")
    # The thread holds what it knew: the record of its script stays for the
    # next process to check, since nothing of the outcome may leave the runner.
    assert [p.name for p in run_log.script_records(tmp_path, "w", "r1")] == [
        f"r1.script.{leader}"]


def test_end_scripts_for_exit_never_signals_a_script_that_already_exited(
        tmp_path, exit_pass, killpg_calls):
    resp = ScriptNodeRunner(str(tmp_path))(_request(tmp_path, "echo $$"))
    pid = int(resp.output.strip())
    assert script_runner._children == set()

    assert script_runner.end_scripts_for_exit() == 0

    assert [c for c in killpg_calls if c[0] == pid] == []
    assert killpg_calls == []


def test_a_script_that_exits_at_the_moment_of_the_pass_has_only_its_own_group_signalled(
        tmp_path, exit_pass, groups, killpg_calls):
    """The script's own process has exited but is not reaped yet (a process
    it left still holds its output open): the pass signals that script's
    group and nothing else, and the script's own exit code is not an outcome."""
    leader_file = tmp_path / "leader"
    record = run_log.runs_root(tmp_path) / "w" / "r1.script.$$"
    # Recorded first: the runner reads the start of a live script only.
    command = (f'for _ in $(seq 500); do [ -e "{record}" ] && break; sleep 0.01; done; '
               f'echo $$ > "{leader_file}"; sleep 61 & exit 0')
    exit_pass.start(ScriptNodeRunner(str(tmp_path)), _request(tmp_path, command))
    leader = _pid_in(leader_file)
    groups.append(leader)
    _until(lambda: _stat(leader).startswith("Z"), "the script's own exit")

    assert script_runner.end_scripts_for_exit() == 1

    assert killpg_calls == [(leader, signal.SIGKILL)]
    _until(lambda: not _group_members(leader), "the group to end")
    _until(lambda: exit_pass.held() == 1, "the runner to wait for the process end")


def _flow(routing: dict, command: str) -> dict:
    return {"name": "w", "start": "s", "nodes": [
        {"id": "s", "kind": "script", "command": command, **routing},
        {"id": "after", "kind": "script", "command": "echo after", "next": None},
        {"id": "other", "kind": "script", "command": "echo other", "next": None}]}


_ROUTING = {
    "linear": {"next": "after"},
    "gate": {"on_pass": "after", "on_fail": "other"},
    "multi-way": {"cases": {"GO": "after", "STOP": "other"}},
}


def _ending(code: int, leader_file: Path) -> str:
    """A script that ends with ``code``: 0 and 1 by itself, with a process it
    left holding its output open so it is not reaped before the pass; -9 by
    the pass, as a script still running when the process ends."""
    if code == -9:
        return f'echo GO; echo $$ > "{leader_file}"; exec sleep 61'
    return f'echo GO; echo $$ > "{leader_file}"; sleep 61 & exit {code}'


@pytest.mark.parametrize("code", [0, 1, -9])
@pytest.mark.parametrize("shape", sorted(_ROUTING))
def test_a_script_ended_by_the_process_exit_is_never_an_outcome(
        tmp_path, exit_pass, groups, shape, code):
    workspace = tmp_path / "ws"
    (workspace / "workflows").mkdir(parents=True)
    leader_file = tmp_path / "leader"
    wf = parse_workflow(_flow(_ROUTING[shape], _ending(code, leader_file)))
    thread = exit_pass.start(engine(workspace).run, wf, "t")
    leader = _pid_in(leader_file)
    groups.append(leader)
    if code != -9:
        _until(lambda: _stat(leader).startswith("Z"), "the script's own exit")

    script_runner.end_scripts_for_exit()

    _until(lambda: exit_pass.held() == 1, "the runner to wait for the process end")
    time.sleep(0.2)          # a thread that went on would have written by now
    assert thread.is_alive()
    record = run_log.read_manifest(workspace, "w", "r1")
    assert record["status"] == "running"
    assert [r["node_id"] for r in record["runs"]] == []
    assert set(marks(workspace)) == {"s"}


def test_a_script_step_entered_after_the_pass_starts_no_process(tmp_path, exit_pass):
    workspace = tmp_path / "ws"
    (workspace / "workflows").mkdir(parents=True)
    assert script_runner.end_scripts_for_exit() == 0
    wf = parse_workflow(_flow(_ROUTING["linear"], "echo started"))

    thread = exit_pass.start(engine(workspace).run, wf, "t")

    _until(lambda: exit_pass.held() == 1, "the runner to wait for the process end")
    assert thread.is_alive()
    assert marks(workspace) == {}                  # the step's command never ran
    assert script_runner._children == set()
    assert run_log.script_records(workspace, "w", "r1") == []
    record = run_log.read_manifest(workspace, "w", "r1")
    assert record["status"] == "running" and record["runs"] == []
