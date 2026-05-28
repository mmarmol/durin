"""Memory background services wiring (audit A11).

Per doc 02 §5.1 + §6.3 and doc 11 audit A11:

- `MemoryFileWatcher` and `HealthCheckScheduler` are started by
  `AgentLoop.__init__` when the corresponding config flag is true
  (both default ON).
- `AgentLoop.stop()` drains them cleanly.
- Failure-to-start is isolated — the agent loop keeps working
  without the optional background service.

Per [[feedback-sync-tests-exercise-behavior]]: these tests
exercise the wiring path with real config + verify the threads
start, the scheduler ticks fire, and stop() cleans up.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from durin.config.schema import (
    MemoryConfig,
    MemoryFileWatcherConfig,
    MemoryHealthCheckConfig,
)


# ---------------------------------------------------------------------------
# config defaults — both services ON by default
# ---------------------------------------------------------------------------


def test_file_watcher_config_default_is_enabled() -> None:
    cfg = MemoryFileWatcherConfig()
    assert cfg.enabled is True


def test_health_check_config_default_is_enabled_with_900s_interval() -> None:
    cfg = MemoryHealthCheckConfig()
    assert cfg.enabled is True
    assert cfg.interval_seconds == 900


def test_memory_config_includes_a11_subsections() -> None:
    cfg = MemoryConfig()
    assert isinstance(cfg.file_watcher, MemoryFileWatcherConfig)
    assert isinstance(cfg.health_check, MemoryHealthCheckConfig)


# ---------------------------------------------------------------------------
# HealthCheckScheduler unit tests
# ---------------------------------------------------------------------------


def test_scheduler_ticks_on_start(tmp_path: Path) -> None:
    """First tick fires immediately (within the wait timeout)."""
    from durin.memory.health_check import (
        HealthCheckScheduler,
        HealthChecker,
    )

    checker = HealthChecker(workspace=tmp_path)
    sched = HealthCheckScheduler(checker, interval_seconds=60)
    sched.start()
    try:
        # The first tick fires before the first wait; poll briefly.
        deadline = time.time() + 2.0
        while time.time() < deadline and sched.tick_count == 0:
            time.sleep(0.02)
        assert sched.tick_count >= 1
    finally:
        sched.stop()


def test_scheduler_stop_is_responsive(tmp_path: Path) -> None:
    """`stop()` returns quickly even though the configured
    interval is large — the inner `wait()` short-circuits."""
    from durin.memory.health_check import (
        HealthCheckScheduler,
        HealthChecker,
    )

    checker = HealthChecker(workspace=tmp_path)
    sched = HealthCheckScheduler(checker, interval_seconds=3600)
    sched.start()
    # Wait for the first tick to settle.
    deadline = time.time() + 2.0
    while time.time() < deadline and sched.tick_count == 0:
        time.sleep(0.02)
    t0 = time.time()
    sched.stop()
    elapsed = time.time() - t0
    assert elapsed < 1.5, (
        f"stop() should return quickly; took {elapsed:.2f}s"
    )


def test_scheduler_isolates_run_tick_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `run_tick()` exception logs but the thread keeps going."""
    from durin.memory.health_check import (
        HealthCheckScheduler,
        HealthChecker,
    )

    checker = HealthChecker(workspace=tmp_path)
    call_count = {"n": 0}

    def flaky_run_tick():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient probe failure")
        return {}

    monkeypatch.setattr(checker, "run_tick", flaky_run_tick)
    # Short interval so the second tick fires quickly enough for the test.
    sched = HealthCheckScheduler(checker, interval_seconds=1)
    sched.start()
    try:
        deadline = time.time() + 3.0
        while time.time() < deadline and call_count["n"] < 2:
            time.sleep(0.05)
        # Both calls happened — the failure did NOT terminate the thread.
        assert call_count["n"] >= 2
    finally:
        sched.stop()


# ---------------------------------------------------------------------------
# AgentLoop wiring — defaults ON, disabled OFF, isolation
# ---------------------------------------------------------------------------


class _LoopShim:
    """Bare AgentLoop substitute that exercises ONLY the A11 wiring
    methods. The 2000+ tests building real `AgentLoop` would pay a
    cost we don't need here — and constructing a real loop requires
    a Provider + ToolsConfig + ... that we don't care about for
    lifecycle correctness."""

    def __init__(self, workspace: Path, app_config: Any) -> None:
        self.workspace = workspace
        self.app_config = app_config
        self._memory_file_watcher: Any | None = None
        self._memory_health_scheduler: Any | None = None
        # Borrow the real methods from AgentLoop unchanged — same
        # behaviour, smaller harness.
        from durin.agent.loop import AgentLoop

        AgentLoop._start_memory_background_services(self)  # type: ignore[arg-type]

    def stop(self) -> None:
        from durin.agent.loop import AgentLoop

        AgentLoop._stop_memory_background_services(self)  # type: ignore[arg-type]


def _build_loop(tmp_path: Path, app_config: Any) -> Any:
    """Construct a loop shim with the A11 wiring exercised."""
    return _LoopShim(tmp_path, app_config)


def _make_app_config(
    watcher_enabled: bool = True,
    health_enabled: bool = True,
    interval: int = 900,
) -> Any:
    """Construct an app_config namespace shaped like DurinConfig but
    minimal — only the bits A11 reads."""
    return SimpleNamespace(
        memory=SimpleNamespace(
            enabled=False,
            file_watcher=MemoryFileWatcherConfig(enabled=watcher_enabled),
            health_check=MemoryHealthCheckConfig(
                enabled=health_enabled, interval_seconds=interval,
            ),
        ),
    )


def test_loop_with_none_config_has_no_services(tmp_path: Path) -> None:
    """`app_config=None` skips A11 wiring entirely — keeps test
    isolation tight for the 2000+ tests that build AgentLoop bare."""
    loop = _build_loop(tmp_path, app_config=None)
    assert loop._memory_file_watcher is None
    assert loop._memory_health_scheduler is None


def test_loop_with_default_config_starts_both_services(
    tmp_path: Path,
) -> None:
    """Defaults (both enabled) → both services wired."""
    cfg = _make_app_config(
        watcher_enabled=True, health_enabled=True, interval=60,
    )
    loop = _build_loop(tmp_path, app_config=cfg)
    try:
        assert loop._memory_file_watcher is not None
        assert loop._memory_health_scheduler is not None
    finally:
        loop.stop()


def test_loop_with_watcher_disabled_skips_watcher(tmp_path: Path) -> None:
    cfg = _make_app_config(watcher_enabled=False, health_enabled=True)
    loop = _build_loop(tmp_path, app_config=cfg)
    try:
        assert loop._memory_file_watcher is None
        assert loop._memory_health_scheduler is not None
    finally:
        loop.stop()


def test_loop_with_health_disabled_skips_scheduler(
    tmp_path: Path,
) -> None:
    cfg = _make_app_config(watcher_enabled=True, health_enabled=False)
    loop = _build_loop(tmp_path, app_config=cfg)
    try:
        assert loop._memory_file_watcher is not None
        assert loop._memory_health_scheduler is None
    finally:
        loop.stop()


def test_loop_stop_drains_both_services(tmp_path: Path) -> None:
    cfg = _make_app_config(
        watcher_enabled=True, health_enabled=True, interval=3600,
    )
    loop = _build_loop(tmp_path, app_config=cfg)
    # Both services running.
    assert loop._memory_file_watcher is not None
    assert loop._memory_health_scheduler is not None
    loop.stop()
    # Both cleared.
    assert loop._memory_file_watcher is None
    assert loop._memory_health_scheduler is None


def test_watcher_start_failure_does_not_break_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the watcher's `start()` raises, the loop still constructs
    AND the health scheduler still runs."""
    from durin.memory.file_watcher import MemoryFileWatcher

    original_start = MemoryFileWatcher.start

    def boom(self) -> None:
        raise RuntimeError("simulated watchdog import or fs error")

    monkeypatch.setattr(MemoryFileWatcher, "start", boom)
    try:
        cfg = _make_app_config(
            watcher_enabled=True, health_enabled=True, interval=60,
        )
        loop = _build_loop(tmp_path, app_config=cfg)
        try:
            assert loop._memory_file_watcher is None  # failed to wire
            assert loop._memory_health_scheduler is not None  # still wired
        finally:
            loop.stop()
    finally:
        monkeypatch.setattr(MemoryFileWatcher, "start", original_start)
