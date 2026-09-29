"""A long blocking job awaited from the event loop gets a thread of its own,
so it never holds one of the default executor's few shared threads."""
from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from durin.utils.dedicated_thread import run_in_dedicated_thread

_VAR: contextvars.ContextVar[str] = contextvars.ContextVar("_VAR", default="unset")


@pytest.mark.asyncio
async def test_runs_even_when_the_default_executor_is_full():
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    gate = threading.Event()
    hog = loop.run_in_executor(None, gate.wait)   # the pool's only thread
    try:
        result = await asyncio.wait_for(
            run_in_dedicated_thread("job", lambda: threading.current_thread().name),
            timeout=5.0)
    finally:
        gate.set()
        await hog
        pool.shutdown()
    assert result == "job"


@pytest.mark.asyncio
async def test_passes_arguments_and_returns_the_result():
    def _job(a, b, *, c):
        return a + b + c

    assert await run_in_dedicated_thread("job", _job, 1, 2, c=3) == 6


@pytest.mark.asyncio
async def test_raises_the_job_exception_in_the_caller():
    def _job():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await run_in_dedicated_thread("job", _job)


@pytest.mark.asyncio
async def test_carries_the_callers_context_variables():
    _VAR.set("from-caller")
    assert await run_in_dedicated_thread("job", _VAR.get) == "from-caller"


@pytest.mark.asyncio
async def test_a_cancelled_caller_leaves_the_job_running_to_completion():
    gate = threading.Event()
    finished = threading.Event()

    def _job():
        gate.wait(5.0)
        finished.set()
        return "late"

    task = asyncio.create_task(run_in_dedicated_thread("job", _job))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    assert await asyncio.to_thread(finished.wait, 5.0)
    # The late result lands on a cancelled future without raising.
    await asyncio.sleep(0.05)


def test_a_job_outliving_its_loop_ends_quietly():
    """A job still running when its event loop closes (a gateway shutdown)
    finishes without raising on its thread."""
    gate = threading.Event()
    thread_errors: list[BaseException] = []
    started: list[threading.Thread] = []
    previous_hook = threading.excepthook
    threading.excepthook = lambda args: thread_errors.append(args.exc_value)

    def _job():
        started.append(threading.current_thread())
        gate.wait(5.0)
        return "late"

    async def _main():
        asyncio.get_running_loop().create_task(run_in_dedicated_thread("job", _job))
        while not started:
            await asyncio.sleep(0.01)

    try:
        asyncio.run(_main())
        gate.set()
        started[0].join(5.0)
    finally:
        threading.excepthook = previous_hook
    assert not started[0].is_alive()
    assert thread_errors == []
