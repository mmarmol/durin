"""Tests for the gateway daemon manager.

We avoid actually spawning a real gateway (slow + flaky). Instead we
drive ``start_daemon`` with a small shim binary that just sleeps,
verifying lifecycle behaviour: PID file write, alive-check, SIGTERM
on stop, stale-PID recovery, doctor integration.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from durin.cli.gateway_daemon import (
    AlreadyRunningError,
    daemon_pid_path,
    daemon_status,
    start_daemon,
    stop_daemon,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect Path.home() so the daemon writes its files inside tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    yield home


def _spawn_dummy(args: list[str]) -> subprocess.Popen:
    """Start a long-running dummy process that we can SIGTERM-test."""
    return subprocess.Popen(
        # sys.executable, not bare "python": the latter is not on PATH in
        # every environment (macOS ships only python3).
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ---------------------------------------------------------------------------
# daemon_status
# ---------------------------------------------------------------------------


def test_daemon_status_not_running_when_no_pid_file(isolated_home: Path) -> None:
    s = daemon_status()
    assert s.state == "not_running"
    assert s.pid is None
    assert not s.is_running


def test_daemon_status_running_when_alive_pid_present(isolated_home: Path) -> None:
    p = _spawn_dummy([])
    try:
        daemon_pid_path().write_text(str(p.pid), encoding="utf-8")
        s = daemon_status()
        assert s.state == "running"
        assert s.pid == p.pid
        assert s.is_running
    finally:
        p.terminate()
        p.wait(timeout=3)


def test_daemon_status_stale_pid_when_process_gone(isolated_home: Path) -> None:
    # Pick a PID that definitely doesn't exist (max+1).
    daemon_pid_path().write_text("999999", encoding="utf-8")
    s = daemon_status()
    assert s.state == "stale_pid"
    assert not s.is_running


def test_daemon_status_stale_pid_when_garbage_contents(isolated_home: Path) -> None:
    daemon_pid_path().write_text("not-a-number", encoding="utf-8")
    s = daemon_status()
    assert s.state == "stale_pid"


# ---------------------------------------------------------------------------
# start_daemon / stop_daemon
# ---------------------------------------------------------------------------


def test_start_daemon_writes_pid_and_returns_alive_process(isolated_home: Path) -> None:
    """Mock _resolve_durin_binary so start_daemon spawns a sleep instead of durin itself."""
    fake_bin = "/bin/sh"

    # Monkeypatch the subprocess invocation: capture what would be spawned,
    # but actually spawn `sleep`.
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        # Replace whatever cmd start_daemon built with a known long sleep.
        return real_popen(["sleep", "30"], **kwargs)

    with patch("durin.cli.gateway_daemon.subprocess.Popen", side_effect=fake_popen), \
         patch("durin.cli.gateway_daemon._resolve_durin_binary", return_value=fake_bin):
        pid = start_daemon([])
    try:
        assert pid > 0
        s = daemon_status()
        assert s.is_running
        assert s.pid == pid
    finally:
        # Cleanup: kill the sleep process.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_start_daemon_rejects_when_already_running(isolated_home: Path) -> None:
    p = _spawn_dummy([])
    try:
        daemon_pid_path().write_text(str(p.pid), encoding="utf-8")
        with pytest.raises(AlreadyRunningError) as excinfo:
            start_daemon([])
        assert excinfo.value.pid == p.pid
    finally:
        p.terminate()
        p.wait(timeout=3)


def test_start_daemon_cleans_stale_pid_and_starts(isolated_home: Path) -> None:
    daemon_pid_path().write_text("999999", encoding="utf-8")  # stale
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        return real_popen(["sleep", "30"], **kwargs)

    with patch("durin.cli.gateway_daemon.subprocess.Popen", side_effect=fake_popen), \
         patch("durin.cli.gateway_daemon._resolve_durin_binary", return_value="/bin/sh"):
        pid = start_daemon([])
    try:
        assert pid > 0
        assert pid != 999999  # the stale entry was discarded
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_stop_daemon_no_op_when_not_running(isolated_home: Path) -> None:
    s = stop_daemon()
    assert s.state == "not_running"


def test_stop_daemon_terminates_running_process(isolated_home: Path) -> None:
    p = _spawn_dummy([])
    try:
        daemon_pid_path().write_text(str(p.pid), encoding="utf-8")
        # Sanity: process should be alive before stop.
        assert daemon_status().is_running
        s = stop_daemon(grace_seconds=3.0)
        assert s.state == "not_running"
        # PID file should be gone.
        assert not daemon_pid_path().exists()
        # The dummy process should be dead too — but waitpid the
        # subprocess so it isn't sitting in zombie state when we check.
        # `wait()` reaps + returns the actual exit code (SIGTERM or
        # SIGKILL — both count as dead).
        p.wait(timeout=3)
        assert p.returncode is not None
    finally:
        try:
            if p.returncode is None:
                p.kill()
                p.wait(timeout=3)
        except Exception:
            pass


def test_stop_daemon_handles_stale_pid_file(isolated_home: Path) -> None:
    daemon_pid_path().write_text("999999", encoding="utf-8")
    s = stop_daemon()
    assert s.state == "not_running"
    assert not daemon_pid_path().exists()


# ---------------------------------------------------------------------------
# doctor integration: gateway daemon + webui checks
# ---------------------------------------------------------------------------


def test_doctor_skips_daemon_check_when_config_disabled(isolated_home: Path) -> None:
    from durin.cli.doctor import check_gateway_daemon
    from durin.config.schema import Config

    cfg = Config()
    cfg.gateway.daemon = False
    with patch("durin.cli.doctor.load_config", return_value=cfg):
        r = check_gateway_daemon()
    assert r.status == "ok"
    assert "disabled" in r.message.lower()


def test_doctor_flags_missing_daemon_when_config_enabled(isolated_home: Path) -> None:
    """`daemon=true` + no PID file → doctor must complain loudly."""
    from durin.cli.doctor import check_gateway_daemon
    from durin.config.schema import Config

    cfg = Config()
    cfg.gateway.daemon = True
    with patch("durin.cli.doctor.load_config", return_value=cfg):
        r = check_gateway_daemon()
    assert r.status == "fail"
    assert "not running" in r.message.lower()
    assert r.fix and "gateway start" in r.fix


def test_doctor_flags_dead_daemon_pid(isolated_home: Path) -> None:
    """Config wants daemon but the PID file points at a dead process."""
    from durin.cli.doctor import check_gateway_daemon
    from durin.config.schema import Config

    daemon_pid_path().write_text("999999", encoding="utf-8")
    cfg = Config()
    cfg.gateway.daemon = True
    with patch("durin.cli.doctor.load_config", return_value=cfg):
        r = check_gateway_daemon()
    assert r.status == "fail"
    assert "dead" in r.message.lower()


def test_doctor_skips_webui_check_when_config_disabled(isolated_home: Path) -> None:
    from durin.cli.doctor import check_webui_reachable
    from durin.config.schema import Config

    cfg = Config()
    cfg.gateway.webui_enabled = False
    with patch("durin.cli.doctor.load_config", return_value=cfg):
        r = check_webui_reachable()
    assert r.status == "ok"
    assert "disabled" in r.message.lower()


def test_doctor_flags_unreachable_webui(isolated_home: Path) -> None:
    """`webui_enabled=true` + nothing listening → fail."""
    from durin.cli.doctor import check_webui_reachable
    from durin.config.schema import Config

    cfg = Config()
    cfg.gateway.webui_enabled = True
    # Point at a port that's basically guaranteed to be closed (high,
    # well outside the default service range). Avoids flakes when the
    # developer machine happens to have something on the default port.
    cfg.channels.__pydantic_extra__["websocket"] = {  # type: ignore[index]
        "host": "127.0.0.1",
        "port": 64999,
    }
    with patch("durin.cli.doctor.load_config", return_value=cfg):
        r = check_webui_reachable(timeout=0.5)
    assert r.status == "fail"
    assert "not reachable" in r.message.lower()
    assert r.fix and "gateway" in r.fix


def test_start_daemon_closes_log_fd_when_popen_fails(tmp_path, monkeypatch):
    """C5: a Popen failure must not leak the log file descriptor."""
    from types import SimpleNamespace

    import durin.cli.gateway_daemon as gd

    monkeypatch.setattr(gd, "daemon_status", lambda: SimpleNamespace(state="none"))
    monkeypatch.setattr(gd, "daemon_logs_path", lambda: tmp_path / "daemon.log")

    opened: list = []
    real_open = open

    def _spy_open(*args, **kwargs):
        f = real_open(*args, **kwargs)
        opened.append(f)
        return f

    monkeypatch.setattr("builtins.open", _spy_open)

    def _boom(*args, **kwargs):
        raise RuntimeError("popen failed")

    monkeypatch.setattr(gd.subprocess, "Popen", _boom)

    with pytest.raises(RuntimeError):
        gd.start_daemon()

    assert opened, "expected the log file to be opened"
    assert all(f.closed for f in opened), "log fd leaked when Popen raised"
