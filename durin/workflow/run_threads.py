"""Workflow runs execute on a bounded pool of threads of their own.

The engine walk is synchronous and can hold its thread for an hour, so a run
never borrows one of the event loop's default-executor threads, which every
short blocking hop in the gateway shares. Nor does each run start a thread of
its own: nothing upstream caps how many runs start at once (API launches,
background ``run_workflow`` calls and ``parallel`` automations are unpaced),
so a burst would become a thread per run. :func:`run_on_workflow_thread`
runs at most :data:`MAX_CONCURRENT_RUNS` at once; the rest wait their turn in
launch order, holding no thread while they wait.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from loguru import logger

__all__ = ["MAX_CONCURRENT_RUNS", "run_on_workflow_thread"]

_T = TypeVar("_T")

# What the paced sources can have running together at their default caps:
# a scheduled automation (or a cron turn's foreground run) runs inside a cron
# job (cron.max_concurrent_jobs, 4) and a chat's foreground run_workflow call
# inside a human-facing turn (agents.defaults.max_concurrent_interactive, 4).
# Both can be full at once without either waiting on the other; only an
# unpaced burst queues.
MAX_CONCURRENT_RUNS = 8

_pool = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_RUNS, thread_name_prefix="workflow-run")
_admitted_lock = threading.Lock()
_admitted = 0  # runs executing or waiting for a thread


def _finished(_future: Future) -> None:
    global _admitted
    with _admitted_lock:
        _admitted -= 1


async def run_on_workflow_thread(
    run_id: str, func: Callable[..., _T], /, *args: Any, **kwargs: Any,
) -> _T:
    """Run ``func(*args, **kwargs)`` on a workflow-run thread, named
    ``workflow-run-<run_id>`` while it runs, and return its result (or raise
    its exception) to the awaiting task.

    Same contract as ``asyncio.to_thread``, on this pool instead of the
    default executor: the caller's context variables are copied in;
    cancelling the awaiting task drops a run still waiting for a thread, and
    leaves a started one running to the end with its result dropped.
    """
    global _admitted
    call = functools.partial(contextvars.copy_context().run, func, *args, **kwargs)

    def _named() -> _T:
        thread = threading.current_thread()
        idle_name, thread.name = thread.name, f"workflow-run-{run_id}"
        try:
            return call()
        finally:
            thread.name = idle_name

    with _admitted_lock:
        ahead = _admitted - MAX_CONCURRENT_RUNS
        future = _pool.submit(_named)
        _admitted += 1
    future.add_done_callback(_finished)
    if ahead >= 0:
        logger.info(
            "workflow run {} waits for a free run thread "
            "({} runs executing, {} waiting ahead of it)",
            run_id, MAX_CONCURRENT_RUNS, ahead)
    return await asyncio.wrap_future(future)
