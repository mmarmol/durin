"""Tests for the hidden `durin memory dream-worker` command."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from durin.cli.memory_cmd import memory_app

runner = CliRunner()


@pytest.fixture()
def worker_env(tmp_path, monkeypatch):
    """Isolated home + workspace; orchestrator functions stubbed."""
    home = tmp_path / "home"
    ws = home / "workspace"
    ws.mkdir(parents=True)
    monkeypatch.setenv("DURIN_HOME", str(home))
    # The command reads workspace_path from config; point config at tmp too.
    import durin.cli.memory_cmd as memory_cmd

    class _Cfg:
        workspace_path = ws

    monkeypatch.setattr(memory_cmd, "load_config", lambda: _Cfg())
    return ws


def test_worker_full_emits_progress_jsonl(worker_env, monkeypatch):
    import durin.memory.dream_orchestrator as orch

    def fake_full(config, workspace, *, progress):
        progress({"kind": "run_started"})
        progress({"kind": "activity", "item": {"t": "x"}})
        progress({"kind": "run_finished", "ok": True})
        return {"sessions": 0}

    monkeypatch.setattr(orch, "run_full_dream", fake_full)
    result = runner.invoke(memory_app, ["dream-worker", "--mode", "full"])
    assert result.exit_code == 0, result.output
    lines = [json.loads(x) for x in result.output.strip().splitlines()]
    assert [x["kind"] for x in lines] == ["run_started", "activity", "run_finished"]


def test_worker_reactive_passes_trigger(worker_env, monkeypatch):
    import durin.memory.dream_orchestrator as orch

    seen = {}

    def fake_reactive(config, workspace, *, trigger, progress):
        seen["trigger"] = trigger
        return {"sessions": 0}

    monkeypatch.setattr(orch, "run_reactive_dream", fake_reactive)
    result = runner.invoke(
        memory_app, ["dream-worker", "--mode", "reactive", "--trigger", "session_close"]
    )
    assert result.exit_code == 0, result.output
    assert seen["trigger"] == "session_close"


def test_worker_failure_exits_1(worker_env, monkeypatch):
    import durin.memory.dream_orchestrator as orch

    def boom(config, workspace, *, progress):
        raise RuntimeError("dream failed")

    monkeypatch.setattr(orch, "run_full_dream", boom)
    result = runner.invoke(memory_app, ["dream-worker", "--mode", "full"])
    assert result.exit_code == 1


def test_worker_lock_held_exits_3(worker_env, monkeypatch):
    import threading

    import durin.memory.dream_orchestrator as orch
    from durin.memory.dream_orchestrator import dream_lock

    called = {"n": 0}

    def fake_full(config, workspace, *, progress):
        called["n"] += 1
        return {}

    monkeypatch.setattr(orch, "run_full_dream", fake_full)

    # Hold the lock from another thread for the duration of the invoke
    # (cross_process_lock is reentrant per thread, so same-thread holding
    # would not contend).
    acquired = threading.Event()
    release = threading.Event()

    def holder():
        with dream_lock(worker_env):
            acquired.set()
            release.wait(timeout=30)

    t = threading.Thread(target=holder)
    t.start()
    assert acquired.wait(timeout=10)
    try:
        result = runner.invoke(memory_app, ["dream-worker", "--mode", "full"])
        assert result.exit_code == 3
        assert called["n"] == 0
    finally:
        release.set()
        t.join(timeout=10)


def test_worker_real_subprocess_smoke(tmp_path):
    """End-to-end: a real `python -m durin memory dream-worker` on an empty
    home completes quickly (no sessions -> no LLM calls) and speaks the
    protocol on stdout."""
    home = tmp_path / "home"
    (home / "workspace" / "sessions").mkdir(parents=True)
    env = {
        **os.environ,
        "DURIN_HOME": str(home),
        # Belt and braces for hermetic subprocess config resolution.
        "HOME": str(tmp_path / "fakehome"),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "durin", "memory", "dream-worker",
         "--mode", "reactive", "--trigger", "smoke"],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    kinds = []
    for line in proc.stdout.strip().splitlines():
        obj = json.loads(line)
        kinds.append(obj["kind"])
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"


def test_worker_writes_persistent_log_file(worker_env, monkeypatch):
    """The worker subprocess persists its own rotating log under
    DURIN_HOME/logs — a 52-minute run must never again be a black box."""
    import durin.memory.dream_orchestrator as orch

    monkeypatch.setattr(
        orch, "run_full_dream",
        lambda config, workspace, *, progress: {"sessions": 0},
    )
    result = runner.invoke(memory_app, ["dream-worker", "--mode", "full"])
    assert result.exit_code == 0, result.output
    from loguru import logger as _logger

    _logger.complete()  # the file sink is enqueue=True; flush before asserting
    log = worker_env.parent / "logs" / "dream-worker.log"
    assert log.is_file() and log.stat().st_size > 0
