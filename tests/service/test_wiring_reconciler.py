"""Periodic run-manifest reconciliation (ghost-run prevention)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

_SWEEPER_NAMES = ("run-reconciler",)      # the periodic sweeper and run-reconciler-build


@pytest.fixture(autouse=True)
def own_sweepers(monkeypatch):
    """Every test here may start sweepers of its own: a fresh once-per-process
    flag and a fresh stop. At the end, on every path, the stop is set and every
    sweeper thread the test started is joined; none may outlive it. Sweepers
    started before the test (by other tests in this process) are left alone."""
    import durin.service.wiring as wiring

    before = set(threading.enumerate())
    stop = threading.Event()
    monkeypatch.setattr(wiring, "_reconciler_started", threading.Event())
    monkeypatch.setattr(wiring, "_reconciler_stop", stop)
    yield
    stop.set()
    started = [t for t in threading.enumerate()
               if t not in before and t.name.startswith(_SWEEPER_NAMES)]
    for thread in started:
        thread.join(timeout=10)
    assert [t.name for t in started if t.is_alive()] == []


def test_periodic_reconciler_flips_dead_owner_run(tmp_path, monkeypatch):
    import durin.service.wiring as wiring
    from durin.workflow import run_log

    run_log.start_run(tmp_path, "wf", "ghost", root_session_key="s",
                      started_at=time.time())
    f = tmp_path / "workflows-runs" / "wf" / "ghost.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec["owner"] = {"pid": 2**22 + 4242, "started": "never"}
    f.write_text(json.dumps(rec), encoding="utf-8")

    # Wait for the sweep that flips the run, then read the manifest once: a
    # read taken while the sweep's thread is rewriting it would test the
    # timing of two threads, not the reconciler.
    flipped = threading.Event()
    real_reconcile = run_log.reconcile_running

    def reconcile_and_report(*args, **kwargs):
        count = real_reconcile(*args, **kwargs)
        if count:
            flipped.set()
        return count

    monkeypatch.setattr(run_log, "reconcile_running", reconcile_and_report)

    assert wiring.start_periodic_run_reconciler(
        lambda: Path(tmp_path), period_s=0.2) is True
    # Second start is a no-op (once per process).
    assert wiring.start_periodic_run_reconciler(
        lambda: Path(tmp_path), period_s=0.2) is False

    assert flipped.wait(timeout=10), "no periodic sweep flipped the dead-owner run"
    assert run_log.read_manifest(tmp_path, "wf", "ghost")["status"] == "crashed"


def test_a_registry_built_on_a_running_loop_sweeps_off_that_loop(tmp_path, monkeypatch):
    """Saving a websocket setting restarts the channel, which builds the
    registry on the gateway's event loop. The sweep there can wait on run locks
    and on the end of a dead run's scripts: it must run off that loop."""
    import asyncio
    from dataclasses import dataclass

    import durin.service.wiring as wiring
    from durin.workflow import run_log

    @dataclass
    class _Sessions:
        workspace: object

    swept: list[tuple[int, bool]] = []
    done = threading.Event()

    def recording(*_a, **_kw):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        swept.append((threading.get_ident(), on_loop))
        done.set()
        return 0

    monkeypatch.setattr(run_log, "reconcile_running", recording)
    started = threading.Event()
    started.set()                       # no periodic sweeper thread from this test
    monkeypatch.setattr(wiring, "_reconciler_started", started)

    async def build_on_the_loop() -> int:
        wiring.build_service_registry(config=None, session_manager=_Sessions(tmp_path))
        return threading.get_ident()

    loop_thread = asyncio.run(build_on_the_loop())
    assert done.wait(timeout=10), "the sweep never ran"
    [(thread, on_loop)] = swept
    assert thread != loop_thread and on_loop is False


def test_a_first_registry_build_on_a_running_loop_starts_the_periodic_sweeper(
    tmp_path, monkeypatch,
):
    """A gateway whose websocket channel is started only later (a hot start)
    builds its first registry on the loop: the periodic sweeper starts then."""
    import asyncio
    from dataclasses import dataclass

    import durin.service.wiring as wiring
    from durin.workflow import run_log

    @dataclass
    class _Sessions:
        workspace: object

    monkeypatch.setattr(run_log, "reconcile_running", lambda *_a, **_kw: 0)
    # The flag is a fresh one (``own_sweepers``): this build is the first.
    assert not wiring._reconciler_started.is_set()

    async def build_on_the_loop() -> None:
        wiring.build_service_registry(config=None, session_manager=_Sessions(tmp_path))

    asyncio.run(build_on_the_loop())
    assert wiring._reconciler_started.is_set()
