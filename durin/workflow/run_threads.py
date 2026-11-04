"""Workflow runs execute on threads of their own.

The engine walk is synchronous and can hold its thread for an hour, so a run
never borrows one of the event loop's default-executor threads, which every
short blocking hop in the gateway shares. How a run gets its thread depends on
what paces it:

- A *paced* run's caller holds a slot of a capped lane for as long as the run
  lasts: a turn waiting on its foreground ``run_workflow`` call (a chat turn
  holds an interactive-lane slot, a scheduled turn its cron job), or a cron
  job firing a scheduled automation. That cap already bounds how many paced
  runs exist, and queueing one would keep its slot, and all the work waiting
  for that lane, idle behind other runs. A paced run starts at once on a
  thread of its own.
- Every other run is unpaced: API launches and resumes, background
  ``run_workflow`` calls, and automations fired by a channel, a chain, a chat
  or by hand start as fast as they arrive, so a burst would become a thread
  per run. Unpaced runs share a pool of at most :data:`MAX_CONCURRENT_RUNS`
  threads; the rest wait their turn in launch order, holding no thread while
  they wait. Paced runs never take a pool thread, so they never wait on one.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from typing import Any, Callable, TypeVar

from loguru import logger

from durin.utils.dedicated_thread import run_in_dedicated_thread
from durin.utils.restart import _flush_logs_before_exit

__all__ = [
    "MAX_CONCURRENT_RUNS", "leave_unfinished_runs", "run_on_workflow_thread", "runs_in_flight",
]

_T = TypeVar("_T")

# Unpaced runs executing at once. Nothing upstream caps those sources, so no
# cap gives this number; eight is what the paced side can hold at its default
# caps (agents.defaults.max_concurrent_interactive, 4, plus
# cron.max_concurrent_jobs, 4), so a burst of unpaced runs takes no more
# threads than paced work can.
MAX_CONCURRENT_RUNS = 8

_pool = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_RUNS, thread_name_prefix="workflow-run")
_admitted_lock = threading.Lock()
_admitted = 0  # unpaced runs executing or waiting for a thread


def _finished(_future: Future) -> None:
    global _admitted
    with _admitted_lock:
        _admitted -= 1


async def run_on_workflow_thread(
    run_id: str, func: Callable[..., _T], /, *args: Any, paced: bool = False,
    **kwargs: Any,
) -> _T:
    """Run ``func(*args, **kwargs)`` on a workflow-run thread, named
    ``workflow-run-<run_id>`` while it runs, and return its result (or raise
    its exception) to the awaiting task.

    ``paced=True`` (the caller holds a capped slot for the whole run, see the
    module docstring) starts a thread of its own at once; otherwise the run
    takes a thread from the bounded pool, waiting for one when all are busy.

    Same contract as ``asyncio.to_thread`` otherwise: the caller's context
    variables are copied in; cancelling the awaiting task drops a run still
    waiting for a pool thread, and leaves a started one running to the end
    with its result dropped.
    """
    if paced:
        return await run_in_dedicated_thread(
            f"workflow-run-{run_id}", func, *args, **kwargs)

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


def runs_in_flight() -> list[str]:
    """The ids of the runs executing on a thread right now, paced or not."""
    prefix = "workflow-run-"
    return sorted(
        thread.name.removeprefix(prefix) for thread in threading.enumerate()
        if thread.name.startswith(prefix))


def leave_unfinished_runs() -> None:
    """End a stopped process now if a run is still executing on its thread.

    A run's thread is no daemon, so an interpreter exit would wait for it, and
    the run could reach nothing while it waits: executors refuse new work once
    the exit begins, so each DNS lookup and thread hop fails, and a provider
    reports that as a connection error and retries until a supervisor kills
    the process. Leaving instead ends the run where a crash would: its manifest
    stays ``running``, and the next start marks it ``crashed`` and tells whoever
    is waiting, the same as after a restart. With no run executing this returns
    and the interpreter exits as usual.
    """
    unfinished = runs_in_flight()
    if not unfinished:
        return
    logger.warning(
        "Shutdown: exiting with {} workflow run(s) unfinished: {}",
        len(unfinished), ", ".join(unfinished))
    _flush_logs_before_exit()
    with suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(0)
