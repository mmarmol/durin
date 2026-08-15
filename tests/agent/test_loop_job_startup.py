"""Gateway startup and the periodic sweep: reconciling orphaned jobs and
picking up queued ones.

``AgentLoop.run()`` resumes OCR work before doing anything else. Rows
``reconcile`` finds orphaned (a dead or long-stale worker pid) get a fresh
worker via ``respawn``; rows left ``queued`` with nothing ever having
claimed them -- a spawn whose own ``Popen`` failed, or a cap-refused
worker's chain-launcher crashing -- get one too, capped at
``MAX_CONCURRENT_OCR_JOBS``. Both loops only ever launch a worker process;
neither claims a job itself (that stays exclusively the worker's own call,
see ``tests/jobs/test_ocr_worker.py``).

The same two loops run again on a timer for the rest of the process's life,
because startup is not the only moment work can strand: a hard-killed worker
leaves its row ``running`` with a dead pid holding the concurrency cap's one
slot, and nothing else re-examines that row until a fresh ingest arrives or
the gateway restarts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger as loguru_logger

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.jobs.registry import JobRegistry
from durin.jobs.spawn import MAX_CONCURRENT_OCR_JOBS


def _make_loop(workspace) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    # Startup runs the job reconcile/pickup block before either of these --
    # noop them so the test only ever exercises that block, not a real MCP
    # connection or the memory embedding warmup.
    loop._connect_mcp = _noop  # type: ignore[method-assign]
    loop._warmup_memory_embedding = _noop  # type: ignore[method-assign]
    return loop


async def _noop(*args, **kwargs):
    return None


async def _run_briefly_and_stop(loop: AgentLoop, *, until) -> None:
    """Start loop.run(), poll a bounded number of times for *until()* to go
    true, then stop the loop. The reconcile/pickup block has no ``await`` in
    it, so it runs to completion the first time the task gets scheduled --
    the bound is only so a regression (the condition never becoming true)
    fails the test instead of hanging it, mirroring the existing
    dispatch-race startup test's own polling shape."""
    runner = asyncio.create_task(loop.run())
    try:
        for _ in range(200):
            await asyncio.sleep(0.005)
            if until():
                break
    finally:
        loop._running = False
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner


def _dead_pid() -> int:
    """A pid that definitely does not belong to a live process: spawn a
    child and wait on it, rather than guessing an arbitrary "probably free"
    integer (mirrors tests/utils/test_process.py's own reaped-child trick)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


def _enqueue(registry: JobRegistry, label: str) -> object:
    return registry.enqueue(
        kind="ocr", label=label,
        payload={"path": f"/tmp/{label}", "pages": [1], "sidecar_dir": None},
        session_key="chat:1", units_total=1,
    )


@asynccontextmanager
async def _live_loop(loop: AgentLoop):
    """Run ``loop.run()`` for the body of the ``async with``, then take it
    down the way a real shutdown does.

    Unlike ``_run_briefly_and_stop`` above, the body runs *after* startup's
    own reconcile/pickup block has finished — the sweep tests build the
    stranded state inside the body precisely so startup cannot be the thing
    that repairs it.
    """
    runner = asyncio.create_task(loop.run())
    try:
        # One scheduling slice is enough: the startup block has no await in
        # it, so it completes the first time the task runs.
        await asyncio.sleep(0.05)
        yield
    finally:
        loop._running = False
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner


async def _wait_for(cond, *, what: str, timeout: float = 5.0) -> None:
    """Poll *cond* until true, or fail naming what never happened. Bounded so
    a regression fails the test instead of hanging it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.005)
        if cond():
            return
    raise AssertionError(f"timed out waiting for {what}")


async def test_startup_launches_a_queued_job_with_no_worker(tmp_path, monkeypatch):
    registry = JobRegistry()
    job = registry.enqueue(
        kind="ocr", label="book.pdf",
        payload={"path": "/tmp/book.pdf", "pages": [1], "sidecar_dir": None},
        session_key="chat:1", units_total=1,
    )
    # Build the loop (which eagerly imports every tool module, transitively
    # including dulwich) *before* Popen is patched -- patching first breaks
    # dulwich's own `proc: subprocess.Popen[bytes]` class-body annotation,
    # since that module has no `from __future__ import annotations` and a
    # plain lambda cannot be subscripted. Exactly the import-order fragility
    # this branch's tests were told to avoid.
    loop = _make_loop(tmp_path)
    calls = []
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: calls.append(args),
    )

    await _run_briefly_and_stop(loop, until=lambda: calls)

    assert calls == [[sys.executable, "-m", "durin.jobs.ocr_worker", job.id]]
    # The launch itself never claims -- that stays the worker's own job.
    assert registry.get(job.id).status == "queued"


async def test_startup_leaves_a_running_job_with_a_live_worker_alone(tmp_path, monkeypatch):
    registry = JobRegistry()
    job = registry.enqueue(
        kind="ocr", label="book.pdf",
        payload={"path": "/tmp/book.pdf", "pages": [1], "sidecar_dir": None},
        session_key="chat:1", units_total=1,
    )
    registry.claim(job.id, pid=os.getpid())  # this test process: guaranteed alive
    loop = _make_loop(tmp_path)  # built before patching Popen -- see the first test
    calls = []
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: calls.append(args),
    )

    # Nothing to wait *for* here -- proving a negative, so just give startup
    # the same bounded window the positive tests use and then check nothing
    # fired. `until` never goes true; the loop exhausts its full budget.
    await _run_briefly_and_stop(loop, until=lambda: False)

    assert calls == []
    reread = registry.get(job.id)
    assert reread.status == "running"
    assert reread.pid == os.getpid()


async def test_startup_respawns_a_dead_workers_orphaned_job(tmp_path, monkeypatch):
    """The pre-existing reconcile -> respawn contract, still exercised through
    the real AgentLoop.run() now that a second loop follows it.

    Two launches for the one job here is the documented interplay, not a
    bug: reconcile flips the orphan back to "queued" and respawns it, and
    since nothing has actually claimed the row yet by the time the second
    loop runs (Popen is mocked -- no real worker process exists to race),
    next_queued("ocr") finds that same still-queued row and launches again.
    In production this second launch is a real subprocess that loses the cap
    race in claim() and exits immediately -- redundant but harmless, exactly
    the tolerance soundness constraint 3 documents for two launched
    processes racing one queued job."""
    registry = JobRegistry()
    job = registry.enqueue(
        kind="ocr", label="book.pdf",
        payload={"path": "/tmp/book.pdf", "pages": [1], "sidecar_dir": None},
        session_key="chat:1", units_total=1,
    )
    registry.claim(job.id, pid=_dead_pid())
    loop = _make_loop(tmp_path)  # built before patching Popen -- see the first test
    calls = []
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: calls.append(args),
    )

    await _run_briefly_and_stop(loop, until=lambda: len(calls) >= 2)

    expected_argv = [sys.executable, "-m", "durin.jobs.ocr_worker", job.id]
    assert calls == [expected_argv, expected_argv]
    reread = registry.get(job.id)
    assert reread.status == "queued"  # reconcile put it back, ready to be claimed
    assert reread.pid is None


async def test_startup_caps_the_queued_pickup_at_the_ocr_limit(tmp_path, monkeypatch):
    """Three queued jobs and no orphans: startup must not launch a worker
    for each one directly -- only MAX_CONCURRENT_OCR_JOBS may, and the chain
    (tests/jobs/test_ocr_worker.py) drains what is left after each finishes."""
    registry = JobRegistry()
    jobs = [
        registry.enqueue(
            kind="ocr", label=f"book{i}.pdf",
            payload={"path": f"/tmp/book{i}.pdf", "pages": [1], "sidecar_dir": None},
            session_key="chat:1", units_total=1,
        )
        for i in range(3)
    ]
    loop = _make_loop(tmp_path)  # built before patching Popen -- see the first test
    calls = []
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: calls.append(args),
    )

    await _run_briefly_and_stop(loop, until=lambda: calls)

    assert len(calls) == MAX_CONCURRENT_OCR_JOBS
    launched_ids = {args[-1] for args in calls}
    assert launched_ids.issubset({j.id for j in jobs})
    # Every job -- launched or not -- is untouched by the launch itself.
    assert {registry.get(j.id).status for j in jobs} == {"queued"}


async def test_startup_queued_pickup_launches_distinct_jobs_at_a_higher_cap(tmp_path, monkeypatch):
    """Important: regression for a duplicate-launch bug that cap=1 hides
    completely. respawn() never claims, so a row the pickup loop just handed
    to it is STILL "queued" afterward -- a loop that re-reads "the oldest
    queued job" on every iteration keeps finding that identical row instead
    of moving on, launching it N times while the rest of the backlog is
    never reached at all. Invisible at cap=1 (a single iteration can't repeat
    anything), which is exactly why this needs its own test at a higher cap
    rather than trusting the cap=1 test above to cover it."""
    monkeypatch.setattr("durin.jobs.spawn.MAX_CONCURRENT_OCR_JOBS", 2)
    registry = JobRegistry()
    jobs = [
        registry.enqueue(
            kind="ocr", label=f"book{i}.pdf",
            payload={"path": f"/tmp/book{i}.pdf", "pages": [1], "sidecar_dir": None},
            session_key="chat:1", units_total=1,
        )
        for i in range(3)
    ]
    loop = _make_loop(tmp_path)  # built before patching Popen -- see the first test
    calls = []
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: calls.append(args),
    )

    await _run_briefly_and_stop(loop, until=lambda: len(calls) >= 2)

    assert len(calls) == 2
    launched_ids = [args[-1] for args in calls]
    assert len(set(launched_ids)) == 2, (
        f"expected 2 distinct jobs launched, got {launched_ids}"
    )
    # The two oldest, specifically -- next_queued/queued_jobs both order
    # oldest-first, and the third (newest) is left for the chain to reach.
    assert set(launched_ids) == {jobs[0].id, jobs[1].id}
    assert {registry.get(j.id).status for j in jobs} == {"queued"}


# --- the periodic sweep: startup's two loops, on a timer -------------------
#
# The two tests that need stranded rows build them BEFORE the loop is live and
# neutralize startup's own pass (see ``_skip_startup_resume``). Building them
# inside the live window instead races the sweep: with the interval patched to
# 10 ms, a tick can land between an ``enqueue`` and its ``claim``, where the
# row is genuinely still queued -- the pickup then launches it correctly, and
# the test reads a state it created itself rather than the one it meant to set
# up. Either way the repair can only come from a sweep tick, which is the
# property these tests exist to hold.


def _skip_startup_resume(monkeypatch) -> None:
    """Make gateway startup's own reconcile/pickup pass a no-op, leaving every
    later call (i.e. every sweep tick) real.

    Needed because the stranded state has to exist before the loop starts (to
    keep the sweep from racing the setup), and startup would otherwise repair
    it first -- leaving the test passing with the sweep body removed entirely,
    which is exactly the regression it must catch. Skipping precisely the
    startup call models the production sequence faithfully: the gateway starts
    against a healthy registry, its pass finds nothing to do, and the worker is
    killed afterwards.
    """
    import durin.agent.loop as loop_module

    real = loop_module._resume_jobs
    seen: list[int] = []

    def resume() -> None:
        seen.append(1)
        if len(seen) == 1:
            return
        real()

    monkeypatch.setattr(loop_module, "_resume_jobs", resume)


async def test_the_sweep_requeues_a_holder_whose_worker_was_killed(tmp_path, monkeypatch):
    """The wedge the cap made possible: a running job's worker is hard-killed
    (SIGKILL, an OOM kill), so it never reaches its chain call, and its row
    keeps ``running`` with a dead pid — holding the cap's only slot. Before
    the sweep, nothing looked at that row again: reconcile ran at startup
    only, the queued job behind it has no worker to chain from, and a
    cap-refused worker's inline self-heal needs a *new* worker to be refused.
    A queue with nothing new arriving stayed wedged until the next restart."""
    monkeypatch.setattr("durin.agent.loop._JOB_SWEEP_INTERVAL_S", 0.01)
    registry = JobRegistry()
    loop = _make_loop(tmp_path)  # built before patching Popen -- see the first test
    # Also before it: _dead_pid spawns a real child, and the patch below lands
    # on the shared `subprocess` module object, not on a per-module alias.
    dead = _dead_pid()
    calls = []
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: calls.append(args),
    )

    # The whole wedge is in place before anything can observe it half-built.
    held = _enqueue(registry, "book.pdf")
    blocked = _enqueue(registry, "next.pdf")
    registry.claim(held.id, pid=dead)
    _skip_startup_resume(monkeypatch)

    async with _live_loop(loop):
        # Waits for the repair itself, not merely for "a launch happened":
        # only reconcile can move this row, so this cannot be satisfied by
        # anything else the sweep does.
        await _wait_for(
            lambda: registry.get(held.id).status == "queued",
            what="the sweep to requeue the dead holder",
        )
        # And then for the launch that follows it: reconcile commits the
        # requeue before respawn appends to `calls`, so the condition above
        # can go true one poll before there is anything to index into.
        await _wait_for(lambda: calls, what="the respawn that follows it")

    reread = registry.get(held.id)
    assert reread.status == "queued"  # the self-heal that had no trigger before
    assert reread.pid is None
    assert calls[0][-1] == held.id
    # `blocked` is still queued, and that is correct at MAX_CONCURRENT_OCR_JOBS
    # == 1: the sweep's pickup takes the OLDEST queued row, which is the one
    # just requeued. The worker it launched claims it and chains to `blocked`
    # when it finishes -- the drain the wedge was blocking.
    assert registry.get(blocked.id).status == "queued"


async def test_the_sweep_picks_up_a_queued_job_nothing_ever_launched(tmp_path, monkeypatch):
    """The other half of the wedge: a row whose own spawn's ``Popen`` failed
    is left ``queued`` with no process anywhere. ``reconcile`` never selects
    it (it only reads ``running`` rows) and no worker exists to chain to it,
    so nothing but a fresh ingest or a restart used to reach it."""
    monkeypatch.setattr("durin.agent.loop._JOB_SWEEP_INTERVAL_S", 0.01)
    registry = JobRegistry()
    loop = _make_loop(tmp_path)  # built before patching Popen -- see the first test
    calls = []
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: calls.append(args),
    )

    # Before the loop, and with startup's pass neutralized -- same reasoning as
    # the wedge test above, so no registry write ever races a sweep tick.
    stranded = _enqueue(registry, "stranded.pdf")
    _skip_startup_resume(monkeypatch)

    async with _live_loop(loop):
        await _wait_for(lambda: calls, what="the sweep to launch the stranded job")

    assert calls[0] == [sys.executable, "-m", "durin.jobs.ocr_worker", stranded.id]
    # Launching still never claims -- that stays the worker's own call.
    assert registry.get(stranded.id).status == "queued"


async def test_a_sweep_that_raises_is_logged_and_the_next_one_still_runs(
    tmp_path, monkeypatch, caplog
):
    """A sweep tick that blows up (a locked database, an unreadable row) must
    not be the last one: the task that would die with it is the only thing
    keeping the queue draining for the rest of the process's life."""
    monkeypatch.setattr("durin.agent.loop._JOB_SWEEP_INTERVAL_S", 0.01)
    ticks = []

    def exploding_resume():
        ticks.append(len(ticks))
        # Call 1 is startup's own (guarded separately); call 2 is the first
        # sweep tick, and it is the one that has to not be the last.
        if len(ticks) == 2:
            raise RuntimeError("jobs.db is locked")

    monkeypatch.setattr("durin.agent.loop._resume_jobs", exploding_resume)
    loop = _make_loop(tmp_path)

    handler_id = loguru_logger.add(caplog.handler, format="{message}", level="ERROR")
    try:
        with caplog.at_level(logging.ERROR):
            async with _live_loop(loop):
                await _wait_for(
                    lambda: len(ticks) >= 4, what="the sweep to survive its own failure",
                )
    finally:
        loguru_logger.remove(handler_id)

    assert len(ticks) >= 4
    assert "periodic job sweep failed" in caplog.text
    # The startup guard's message is a different one; the sweep's own failure
    # must not be reported as a startup failure.
    assert "job reconcile at startup failed" not in caplog.text


async def test_shutting_the_loop_down_cancels_the_sweep(tmp_path, monkeypatch):
    """The task must not outlive the loop that started it -- a sweep still
    firing against a torn-down gateway would launch workers nobody owns."""
    monkeypatch.setattr("durin.agent.loop._JOB_SWEEP_INTERVAL_S", 0.01)
    loop = _make_loop(tmp_path)
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: None,
    )

    async with _live_loop(loop):
        await _wait_for(
            lambda: loop._job_sweep_task is not None, what="the sweep task to start",
        )
        sweep = loop._job_sweep_task

    # `_live_loop` cancels run() exactly as a shutdown does; the sweep goes
    # with it rather than being left pending on a closing loop.
    await asyncio.sleep(0.01)
    assert sweep.done()
    assert loop._job_sweep_task is None


async def test_stop_cancels_the_sweep_too(tmp_path, monkeypatch):
    """``stop()`` tears the sweep down alongside the loop's other background
    services, so a caller that stops without cancelling ``run()`` (the TUI's
    own shutdown path) does not leave it running either."""
    monkeypatch.setattr("durin.agent.loop._JOB_SWEEP_INTERVAL_S", 0.01)
    loop = _make_loop(tmp_path)
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: None,
    )
    runner = asyncio.create_task(loop.run())
    try:
        await _wait_for(
            lambda: loop._job_sweep_task is not None, what="the sweep task to start",
        )
        sweep = loop._job_sweep_task

        loop.stop()

        await asyncio.sleep(0.01)
        assert sweep.done()
        assert loop._job_sweep_task is None
    finally:
        loop._running = False
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner


# --- the retention day-tick: pruning rides the same pass, at most daily ----


def test_the_day_tick_prunes_once_per_day_not_once_per_pass(monkeypatch):
    """The pass runs every minute (and once at startup), but retention has a
    30-day window — nothing about it needs re-checking 1440 times a day. The
    day-tick state and the injectable clock are what keep this test
    synchronous: no sweep task, no sleeps."""
    import durin.agent.loop as loop_module
    from durin.jobs.registry import _JOB_RETENTION_S

    calls = []
    monkeypatch.setattr(
        JobRegistry, "prune_terminal",
        lambda self, older_than_s, *, now=None: calls.append(older_than_s) or 0,
    )
    monkeypatch.setattr(loop_module, "_last_job_prune_at", None)  # fresh process
    registry = JobRegistry()
    t0 = 1_700_000_000.0

    loop_module._prune_jobs_if_due(registry, now=t0)  # first pass: due at once
    loop_module._prune_jobs_if_due(registry, now=t0 + 60)  # the next sweep tick
    loop_module._prune_jobs_if_due(registry, now=t0 + 23 * 3600)  # still same day

    assert calls == [_JOB_RETENTION_S]  # one prune, with the retention window

    loop_module._prune_jobs_if_due(registry, now=t0 + 24 * 3600 + 1)  # a day later

    assert calls == [_JOB_RETENTION_S, _JOB_RETENTION_S]  # exactly one more


def test_a_prune_failure_does_not_cost_the_pass_its_respawn_and_pickup_duties(
    tmp_path, monkeypatch,
):
    """Retention is the pass's least important duty: a prune that blows up (a
    locked database) must be guarded on its own, exactly like each respawn
    is, so the reconcile/respawn and queued-pickup work still happens in the
    same pass — and _resume_jobs must not raise, or the sweep would log the
    wrong failure."""
    import durin.agent.loop as loop_module

    registry = JobRegistry()
    dead = _dead_pid()  # before Popen is patched — it spawns a real child
    held = _enqueue(registry, "book.pdf")
    registry.claim(held.id, pid=dead)

    pruned = []

    def _boom(self, older_than_s, *, now=None):
        pruned.append(older_than_s)
        raise RuntimeError("jobs.db is locked")

    monkeypatch.setattr(JobRegistry, "prune_terminal", _boom)
    monkeypatch.setattr(loop_module, "_last_job_prune_at", None)  # prune is due
    calls = []
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: calls.append(args),
    )

    loop_module._resume_jobs()  # must not raise

    assert pruned  # the prune genuinely ran (and blew up) inside this pass
    reread = registry.get(held.id)
    assert reread.status == "queued"  # reconcile still requeued the dead holder
    assert reread.pid is None
    # And both launches still happened: reconcile's respawn of the orphan,
    # then the pickup finding the same still-queued row (the documented
    # redundancy the claim() status guard makes harmless).
    assert [args[-1] for args in calls] == [held.id, held.id]
