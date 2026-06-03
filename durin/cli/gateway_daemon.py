"""Daemonization + lifecycle helpers for ``durin gateway``.

Why this lives in its own module: the foreground gateway path
(``_run_gateway``) is large and async-heavy. Daemon mode is a thin
shell around it — fork off, redirect IO, write a PID file, exit.
Keeping it separate makes the lifecycle easy to test without touching
the gateway runtime itself.

Lifecycle:

- ``start_daemon(...)`` — spawns a detached subprocess that runs the
  same ``durin gateway`` command in foreground mode under the hood,
  writes a PID file, exits the parent.
- ``stop_daemon()`` — reads the PID file, sends SIGTERM, escalates to
  SIGKILL after a grace period, removes the PID file.
- ``daemon_status()`` — returns a typed report (running / not running /
  stale PID) including the live PID, port, and log path.
- ``daemon_logs_path()`` — where the daemon's stdout/stderr land.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

__all__ = [
    "DaemonStatus",
    "start_daemon",
    "stop_daemon",
    "daemon_status",
    "daemon_pid_path",
    "daemon_logs_path",
    "daemon_boot_logs_path",
    "GATEWAY_LOG_FILE_ENV",
]


# Env flag set by start_daemon and read by the gateway run to decide
# whether to attach the JSONL rotating file sink to gateway.log.
GATEWAY_LOG_FILE_ENV = "DURIN_GATEWAY_LOG_FILE"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _state_root() -> Path:
    """Where we keep the PID + log files. Stable across the agent's lifetime."""
    root = Path.home() / ".durin"
    root.mkdir(parents=True, exist_ok=True)
    return root


def daemon_pid_path() -> Path:
    return _state_root() / "gateway.pid"


def daemon_logs_path() -> Path:
    logs = _state_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "gateway.log"


def daemon_boot_logs_path() -> Path:
    """Raw stdout/stderr capture for the daemon child (truncated each start).

    Catches catastrophic pre-loguru failures (import errors, early
    tracebacks). The structured log lives in ``gateway.log`` (loguru-owned,
    rotating); this file is only a boot-time safety net.
    """
    logs = _state_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "gateway.boot.log"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonStatus:
    state: Literal["running", "not_running", "stale_pid"]
    pid: int | None
    pid_file: Path
    log_file: Path

    @property
    def is_running(self) -> bool:
        return self.state == "running"


def _pid_alive(pid: int) -> bool:
    """Best-effort check: does this PID still belong to a live process?"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists, owned by someone else. Treat as alive.
        return True


def _read_pid(pid_path: Path) -> int | None:
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def daemon_status() -> DaemonStatus:
    pid_path = daemon_pid_path()
    log_path = daemon_logs_path()
    if not pid_path.exists():
        return DaemonStatus("not_running", None, pid_path, log_path)
    pid = _read_pid(pid_path)
    if pid is None:
        return DaemonStatus("stale_pid", None, pid_path, log_path)
    if _pid_alive(pid):
        return DaemonStatus("running", pid, pid_path, log_path)
    return DaemonStatus("stale_pid", pid, pid_path, log_path)


# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------


class AlreadyRunningError(RuntimeError):
    """Raised when start_daemon is called and a live gateway exists."""

    def __init__(self, pid: int) -> None:
        super().__init__(f"gateway is already running (pid {pid})")
        self.pid = pid


def start_daemon(
    extra_args: list[str] | None = None,
    *,
    durin_executable: str | None = None,
) -> int:
    """Detach a background gateway. Returns the PID of the spawned process.

    Raises :class:`AlreadyRunningError` if a live daemon is found.
    Stale PID files (process gone) are cleaned up first.
    """
    status = daemon_status()
    if status.state == "running":
        raise AlreadyRunningError(status.pid or -1)
    if status.state == "stale_pid":
        # Clean up before starting fresh.
        try:
            status.pid_file.unlink()
        except OSError:
            pass

    # loguru OWNS gateway.log (rotation/compression). The parent must not
    # also hold an fd to it, so the child's raw stdout/stderr go to a
    # separate boot file (truncated each start) — a safety net for
    # catastrophic pre-loguru output (import errors, early tracebacks).
    boot_path = daemon_boot_logs_path()
    boot_path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = open(boot_path, "wb", buffering=0)  # noqa: SIM115 — kept open for the child
    # try/finally so the fd is closed even if Popen raises (C5): the child
    # has already dup'd it, so the parent always drops its copy.
    try:
        binary = durin_executable or _resolve_durin_binary()
        cmd = [binary, "gateway", "--foreground", *(extra_args or [])]
        env = {**os.environ, GATEWAY_LOG_FILE_ENV: str(daemon_logs_path())}
        proc = subprocess.Popen(  # noqa: S603 — durin invokes its own binary; no shell
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    finally:
        log_fd.close()

    pid_path = daemon_pid_path()
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    return proc.pid


def _resolve_durin_binary() -> str:
    """Find the ``durin`` executable to re-invoke for the child.

    Falls back to ``sys.executable -m durin.cli.commands`` if the
    durin script isn't on PATH (rare; covers oddball editable installs).
    """
    import shutil

    found = shutil.which("durin")
    if found:
        return found
    # Last resort: re-invoke via the current interpreter.
    return f"{sys.executable} -m durin.cli.commands"


def stop_daemon(*, grace_seconds: float = 5.0) -> DaemonStatus:
    """Send SIGTERM to the daemon and wait up to ``grace_seconds`` for it to exit.

    Escalates to SIGKILL if the process is still alive after the grace
    window. Returns the final :class:`DaemonStatus` after cleanup.
    """
    status = daemon_status()
    if status.state == "not_running":
        return status
    if status.pid is None:
        # Stale PID file with garbage contents — just clean up.
        try:
            status.pid_file.unlink()
        except OSError:
            pass
        return DaemonStatus("not_running", None, status.pid_file, status.log_file)

    pid = status.pid
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            status.pid_file.unlink()
        except OSError:
            pass
        return DaemonStatus("not_running", None, status.pid_file, status.log_file)

    # Poll until the process exits or the grace window expires.
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        status.pid_file.unlink()
    except OSError:
        pass
    return DaemonStatus("not_running", None, status.pid_file, status.log_file)
