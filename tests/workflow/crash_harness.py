"""Run a workflow in a real child process and end that process while a chosen
step is running, so a test sees exactly what a dead process leaves on disk.

Layout under the test's ``tmp_path``: the workspace is ``ws`` (the definition is
written to ``ws/workflows/<name>.json``, so a test can place other workflows
beside it before the run), ``home``, ``durin-home`` and ``tmp`` are the child's
``HOME``, ``DURIN_HOME`` and ``TMPDIR`` (a killed child leaves the private
copies of its parallel branches in its run's folder under ``ws/.workflow/``, not
in ``tmp``), and ``driver.log`` collects the child's output.
``env`` adds the driver's switches (``_crash_driver``) to the child's environment.

Every process a test starts here is tracked, and ``reap`` (run after each test
by the autouse fixture in this package's conftest) kills whatever is still
alive: the child itself when it outlived the call (``how="exec"``), and the
processes it left behind. Script steps run in their own session, so killing the
child alone would leave a hung script sleeping.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from durin.utils.process_tree import process_identity, process_state
from durin.workflow import run_log
from durin.workflow.engine import WorkflowEngine
from tests.workflow import _crash_driver as driver

# Ends the child right after the k-th real file of a parallel step's write-back
# into the working folder is written (``env={HANG_IN_APPLY_ENV: "<k>"}``, with
# ``stop_in=None``).
HANG_IN_APPLY_ENV = driver.HANG_IN_APPLY_ENV

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER = Path(driver.__file__).resolve()
_HUNG_WAIT_S = 30.0       # the step asked for must start within this
_EXIT_WAIT_S = 30.0       # a signalled child must be gone within this
_RUN_WAIT_S = 60.0        # a run to its end must finish within this
_EXEC_WAIT_S = 10.0       # a replaced image must show within this
_POLL_S = 0.05

# Every child started, and the identities of processes they left behind.
_children: list[subprocess.Popen] = []
_strays: list[dict] = []


@dataclass
class Dead:
    workspace: Path          # the workspace the child ran in
    run_id: str              # always "r1"
    record: dict             # the record as the child left it (status "running")
    marks: dict[str, int]    # how many times each step started, read from marker files


def marks(workspace: Path) -> dict[str, int]:
    """How many times each step started. Only plain step-id files count: the
    ``.hung`` flag and any ``<id>.<suffix>`` side file are not starts."""
    folder = driver.marks_dir(workspace)
    if not folder.is_dir():
        return {}
    return {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in folder.iterdir()
        if path.is_file() and "." not in path.name
    }


def engine(workspace: Path, run_id: str = driver.RUN_ID) -> WorkflowEngine:
    """An engine in THIS process with the same stand-in agent runner and the real
    script runner the child uses, on the same workspace and run id."""
    return driver.build_engine(workspace, run_id)


def run_to_end(tmp_path: Path, definition: dict, task: str = "t", *,
               env: dict[str, str] | None = None) -> tuple[dict, dict[str, int]]:
    """Run `definition` to its end in a child process; returns (record, marks)."""
    workspace, proc = _launch(Path(tmp_path), definition, task, hang_in=None, env=env)
    try:
        proc.wait(timeout=_RUN_WAIT_S)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"the run did not end within {_RUN_WAIT_S:.0f}s\n{_log(tmp_path)}") from None
    if proc.returncode != 0:
        raise AssertionError(f"the child exited with {proc.returncode}\n{_log(tmp_path)}")
    return _record(workspace, definition, _run_id(env)), marks(workspace)


def run_until_killed(tmp_path: Path, definition: dict, *, stop_in: str | None,
                     how: str = "kill", task: str = "t",
                     env: dict[str, str] | None = None,
                     keep_scripts: bool = False) -> Dead:
    """Run `definition` in a child process and end the process while step
    `stop_in` is running (None: while the step `env` makes hang, such as a
    fan-out worker by its task, or inside a write-back with `HANG_IN_APPLY_ENV`).
    `how`: "kill" (SIGKILL), "term" (SIGTERM, then wait), "exec" (the child
    replaces its own image, keeping its pid).

    The script processes the child leaves behind are killed at once, unless
    `keep_scripts`: they then stay alive for the test, which is what a dead
    process leaves in production, and ``reap`` still kills any left after it."""
    if how not in ("kill", "term", "exec"):
        raise ValueError(f"how must be 'kill', 'term' or 'exec', got {how!r}")
    tmp_path = Path(tmp_path)
    workspace, proc = _launch(tmp_path, definition, task, hang_in=stop_in, env=env)
    _wait_until_hung(tmp_path, workspace, proc, stop_in or "(set by env)")
    # Snapshot before signalling: once the child is gone its orphans are
    # re-parented and can no longer be told apart from anyone else's. A
    # descendant that already exited has no start time and is left out, so a
    # later process given its pid is never mistaken for it.
    for pid in _descendants(proc.pid):
        identity = process_identity(pid)
        if not identity.get("started"):
            continue
        try:
            identity["pgid"] = os.getpgid(pid)
        except OSError:
            continue
        _strays.append(identity)
    if how == "exec":
        proc.send_signal(signal.SIGUSR1)
        _wait_until_replaced(tmp_path, proc)
    else:
        proc.send_signal(signal.SIGKILL if how == "kill" else signal.SIGTERM)
        try:
            proc.wait(timeout=_EXIT_WAIT_S)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"the child outlived {how} for {_EXIT_WAIT_S:.0f}s\n{_log(tmp_path)}") from None
        if not keep_scripts:
            _kill_strays()
    run_id = _run_id(env)
    return Dead(workspace=workspace, run_id=run_id,
                record=_record(workspace, definition, run_id), marks=marks(workspace))


def reap() -> None:
    """Kill and wait for every child still alive, and kill what they left behind."""
    while _children:
        proc = _children.pop()
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=_EXIT_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
    _kill_strays()


def _run_id(env: dict[str, str] | None) -> str:
    return (env or {}).get(driver.RUN_ID_ENV) or driver.RUN_ID


def _launch(tmp_path: Path, definition: dict, task: str, *, hang_in: str | None,
            env: dict[str, str] | None = None):
    workspace = tmp_path / "ws"
    flows = workspace / "workflows"
    flows.mkdir(parents=True, exist_ok=True)
    path = flows / f"{definition['name']}.json"
    path.write_text(json.dumps(definition), encoding="utf-8")
    # A flag left by an earlier child on this workspace must not satisfy the wait.
    (driver.marks_dir(workspace) / ".hung").unlink(missing_ok=True)
    home, durin_home, tmp = tmp_path / "home", tmp_path / "durin-home", tmp_path / "tmp"
    for folder in (home, durin_home, tmp):
        folder.mkdir(exist_ok=True)
    child_env = {
        "HOME": str(home),
        "DURIN_HOME": str(durin_home),
        "TMPDIR": str(tmp),
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONPATH": str(_REPO_ROOT),
        "PATH": os.environ.get("PATH", ""),
        **(env or {}),
    }
    if hang_in is not None:
        child_env[driver.HANG_ENV] = hang_in
    with open(tmp_path / "driver.log", "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-I", str(_DRIVER), str(workspace), str(path), task],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=child_env, cwd=str(tmp_path))
    _children.append(proc)
    return workspace, proc


def _wait_until_hung(tmp_path: Path, workspace: Path, proc: subprocess.Popen, step: str) -> None:
    flag = driver.marks_dir(workspace) / ".hung"
    deadline = time.monotonic() + _HUNG_WAIT_S
    while not flag.exists():
        if proc.poll() is not None:
            raise AssertionError(
                f"the child exited with {proc.returncode} before step {step!r} started"
                f"\n{_log(tmp_path)}")
        if time.monotonic() > deadline:
            raise AssertionError(
                f"step {step!r} did not start within {_HUNG_WAIT_S:.0f}s\n{_log(tmp_path)}")
        time.sleep(_POLL_S)


def _wait_until_replaced(tmp_path: Path, proc: subprocess.Popen) -> None:
    """Return once the pid runs the replacement image, so the record is read
    after the original process is gone."""
    deadline = time.monotonic() + _EXEC_WAIT_S
    while "time.sleep(120)" not in _command_of(proc.pid):
        if proc.poll() is not None:
            raise AssertionError(
                f"the child exited with {proc.returncode} instead of replacing its image"
                f"\n{_log(tmp_path)}")
        if time.monotonic() > deadline:
            raise AssertionError(
                f"the child did not replace its image within {_EXEC_WAIT_S:.0f}s\n{_log(tmp_path)}")
        time.sleep(_POLL_S)


def _command_of(pid: int) -> str:
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                         capture_output=True, text=True, timeout=10)
    return out.stdout


def _descendants(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="],
                         capture_output=True, text=True, timeout=10)
    children: dict[int, list[int]] = {}
    for line in out.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2:
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
    found, pending = [], [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found


def _kill_strays() -> None:
    # Killed only while still the same process (pid and start time), never a
    # stranger that reused the pid. A script step leads its own process group,
    # and a process it forked after the snapshot is only reachable through
    # that group: a stray that led its group at the snapshot has the group
    # killed while the group still has members, even when the stray itself is
    # gone (a group's id is never given out while it has members).
    leaders: list[int] = []
    while _strays:
        stray = _strays.pop()
        pid = stray["pid"]
        if stray.get("pgid") == pid:
            leaders.append(pid)
        # Only on proof it is still that process: one that cannot be observed
        # is never taken for it.
        if process_state(pid, stray.get("started")) == "alive":
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for pgid in leaders:
        if _group_has_members(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _group_has_members(pgid: int) -> bool:
    """Whether group ``pgid`` has a member that has not exited, read with ps."""
    out = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True,
                         text=True, timeout=10).stdout
    return any(parts[0] == str(pgid) and not parts[1].startswith("Z")
               for parts in (line.split() for line in out.splitlines()) if len(parts) >= 2)


def _record(workspace: Path, definition: dict, run_id: str = driver.RUN_ID) -> dict:
    record = run_log.read_manifest(workspace, definition["name"], run_id)
    assert record is not None, f"no record for run {run_id!r} in {workspace}"
    return record


def _log(tmp_path: Path) -> str:
    try:
        return (tmp_path / "driver.log").read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return ""
