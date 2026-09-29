"""Await a long blocking job on a thread of its own.

``asyncio.to_thread`` borrows a thread from the event loop's default
executor, which every short blocking hop in the gateway shares (file I/O,
``flock``, synchronous SDK calls) and which is sized from the CPU count —
``min(32, cpus + 4)``, six threads on a two-CPU host. A job that holds its
thread for minutes or hours (supervising a dream) shrinks that pool for its
whole run; a few of them at once leave every other hop queued behind them.
:func:`run_in_dedicated_thread` starts a named thread per job instead, so long
jobs never compete with short hops for the pool. It suits jobs something
upstream already paces; workflow runs nothing paces take a bounded pool of
their own instead (``durin.workflow.run_threads``).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from typing import Any, Callable, TypeVar

__all__ = ["run_in_dedicated_thread"]

_T = TypeVar("_T")


async def run_in_dedicated_thread(
    name: str, func: Callable[..., _T], /, *args: Any, **kwargs: Any,
) -> _T:
    """Run ``func(*args, **kwargs)`` on a new thread called ``name`` and
    return its result (or raise its exception) to the awaiting task.

    Same contract as ``asyncio.to_thread`` otherwise: the caller's context
    variables are copied into the thread, and cancelling the awaiting task
    does not stop the job — it runs to the end and its result is dropped.
    The thread is not a daemon, like the default executor's workers: an
    interpreter exit waits for the job to finish.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_T] = loop.create_future()
    call = functools.partial(contextvars.copy_context().run, func, *args, **kwargs)

    def _settle(setter: Callable[[Any], None], value: Any) -> None:
        # The awaiting task was cancelled: nobody wants the outcome.
        if not future.done():
            setter(value)

    def _deliver(setter: Callable[[Any], None], value: Any) -> None:
        try:
            loop.call_soon_threadsafe(_settle, setter, value)
        except RuntimeError:
            # The loop closed while the job ran (a shutdown); nobody waits.
            pass

    def _run() -> None:
        try:
            result = call()
        except BaseException as exc:  # noqa: BLE001 - handed to the awaiting task
            _deliver(future.set_exception, exc)
        else:
            _deliver(future.set_result, result)

    threading.Thread(target=_run, name=name).start()
    return await future
