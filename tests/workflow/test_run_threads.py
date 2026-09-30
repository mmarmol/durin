"""Workflow runs execute on a bounded pool of threads of their own: never on
the event loop's shared default executor, and never more than the bound at
once, however many are launched together."""
from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from loguru import logger

from durin.workflow.run_threads import MAX_CONCURRENT_RUNS, run_on_workflow_thread

_VAR: contextvars.ContextVar[str] = contextvars.ContextVar("_VAR", default="unset")


async def _until(predicate, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_a_burst_runs_at_most_the_bound_at_once_and_every_run_finishes():
    gate = threading.Event()
    lock = threading.Lock()
    counts = {"running": 0, "peak": 0}

    def _run(i):
        with lock:
            counts["running"] += 1
            counts["peak"] = max(counts["peak"], counts["running"])
        try:
            gate.wait(10.0)
            return i
        finally:
            with lock:
                counts["running"] -= 1

    burst = MAX_CONCURRENT_RUNS + 3
    runs = [asyncio.create_task(run_on_workflow_thread(f"r{i}", _run, i)) for i in range(burst)]
    try:
        assert await _until(lambda: counts["running"] >= MAX_CONCURRENT_RUNS)
        await asyncio.sleep(0.3)   # time for any run not held back to start too
        peak = counts["peak"]
    finally:
        gate.set()
    assert await asyncio.wait_for(asyncio.gather(*runs), timeout=10.0) == list(range(burst))
    assert peak == MAX_CONCURRENT_RUNS


@pytest.mark.asyncio
async def test_a_paced_run_starts_at_once_while_unpaced_runs_fill_the_pool():
    """A paced run's caller holds a capped slot (a turn, a cron job) for the
    whole run: it never queues behind unpaced runs, and it takes no pool
    thread either."""
    gate = threading.Event()
    holders = [asyncio.create_task(run_on_workflow_thread(f"h{i}", gate.wait, 10.0))
               for i in range(MAX_CONCURRENT_RUNS)]
    try:
        await asyncio.sleep(0.05)
        name = await asyncio.wait_for(
            run_on_workflow_thread("fg", lambda: threading.current_thread().name, paced=True),
            timeout=2.0)
        queued = asyncio.create_task(run_on_workflow_thread("late", lambda: "ok"))
        await asyncio.sleep(0.1)
        assert not queued.done()          # the pool is still full of unpaced runs
    finally:
        gate.set()
    assert name == "workflow-run-fg"
    assert await asyncio.wait_for(queued, timeout=10.0) == "ok"
    await asyncio.wait_for(asyncio.gather(*holders), timeout=10.0)


@pytest.mark.asyncio
async def test_runs_even_when_the_default_executor_is_full():
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    gate = threading.Event()
    hog = loop.run_in_executor(None, gate.wait)   # the pool's only thread
    try:
        result = await asyncio.wait_for(
            run_on_workflow_thread("abc", lambda: "ran"), timeout=5.0)
    finally:
        gate.set()
        await hog
        pool.shutdown()
    assert result == "ran"


@pytest.mark.asyncio
async def test_the_thread_carries_the_run_id_while_the_run_executes():
    seen: list[threading.Thread] = []

    def _run():
        seen.append(threading.current_thread())
        return threading.current_thread().name

    assert await run_on_workflow_thread("abc123", _run) == "workflow-run-abc123"
    assert seen[0].name != "workflow-run-abc123"   # back to its idle name


@pytest.mark.asyncio
async def test_passes_arguments_and_the_callers_context_and_returns_the_result():
    _VAR.set("from-caller")

    def _run(a, b, *, c):
        return a + b + c, _VAR.get()

    assert await run_on_workflow_thread("r", _run, 1, 2, c=3) == (6, "from-caller")


@pytest.mark.asyncio
async def test_raises_the_run_exception_in_the_caller():
    def _run():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await run_on_workflow_thread("r", _run)


@pytest.mark.asyncio
async def test_a_run_cancelled_while_it_waits_for_a_thread_never_starts():
    gate = threading.Event()
    started = threading.Event()
    holders = [asyncio.create_task(run_on_workflow_thread(f"h{i}", gate.wait, 10.0))
               for i in range(MAX_CONCURRENT_RUNS)]
    waiting = asyncio.create_task(run_on_workflow_thread("late", started.set))
    try:
        await asyncio.sleep(0.1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    finally:
        gate.set()
    await asyncio.wait_for(asyncio.gather(*holders), timeout=10.0)
    # Every thread is free again, and the cancelled run never took one.
    assert await run_on_workflow_thread("after", lambda: "ok") == "ok"
    assert not started.is_set()


@pytest.mark.asyncio
async def test_a_run_that_has_to_wait_says_so():
    gate = threading.Event()
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="INFO", format="{message}",
                      filter="durin.workflow.run_threads")
    holders = [asyncio.create_task(run_on_workflow_thread(f"h{i}", gate.wait, 10.0))
               for i in range(MAX_CONCURRENT_RUNS)]
    try:
        await asyncio.sleep(0.05)
        assert not lines                  # the bound itself admits them all
        queued = asyncio.create_task(run_on_workflow_thread("late", lambda: "ok"))
        await asyncio.sleep(0.05)
    finally:
        gate.set()
        logger.remove(sink)
    assert await asyncio.wait_for(queued, timeout=10.0) == "ok"
    await asyncio.wait_for(asyncio.gather(*holders), timeout=10.0)
    assert len(lines) == 1
    assert "workflow run late waits for a free run thread" in lines[0]


def test_a_run_outliving_its_loop_ends_quietly():
    """A run still executing when its event loop closes (a gateway shutdown)
    finishes without raising on its thread."""
    gate = threading.Event()
    thread_errors: list[BaseException] = []
    started = threading.Event()
    finished = threading.Event()
    previous_hook = threading.excepthook
    threading.excepthook = lambda args: thread_errors.append(args.exc_value)

    def _run():
        started.set()
        gate.wait(5.0)
        finished.set()
        return "late"

    async def _main():
        asyncio.get_running_loop().create_task(run_on_workflow_thread("r", _run))
        while not started.is_set():
            await asyncio.sleep(0.01)

    try:
        asyncio.run(_main())
        gate.set()
        assert finished.wait(5.0)
    finally:
        threading.excepthook = previous_hook
    assert thread_errors == []
