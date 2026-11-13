"""Workflow runs execute on a bounded pool of threads of their own: never on
the event loop's shared default executor, and never more than the bound at
once, however many are launched together."""
from __future__ import annotations

import asyncio
import contextvars
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from loguru import logger

from durin.workflow import run_threads
from durin.workflow.run_threads import MAX_CONCURRENT_RUNS, run_on_workflow_thread

# The real function, held from import time: the suite-wide guard replaces the
# module's name with a no-op for every test, so no test can end its process.
_REAL_LEAVE_UNFINISHED_RUNS = run_threads.leave_unfinished_runs

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


_STOPPED_GATEWAY = """
import asyncio
import sys
import threading

from durin.workflow.run_threads import leave_unfinished_runs, run_on_workflow_thread

paced = sys.argv[1] == "paced"
started = threading.Event()


def _run():
    started.set()
    threading.Event().wait()


async def _main():
    asyncio.get_running_loop().create_task(
        run_on_workflow_thread("unfinished", _run, paced=paced))
    while not started.is_set():
        await asyncio.sleep(0.01)


asyncio.run(_main())
print("stopped", flush=True)
leave_unfinished_runs()
print("exited on its own", flush=True)
"""


@pytest.mark.parametrize("pace", ["paced", "unpaced"])
def test_a_stopped_gateway_exits_with_a_run_unfinished(pace):
    """An interpreter exit waits for a run's thread, and a run on that thread
    can reach nothing, since executors refuse work once the exit has begun: a
    stop has to end the process instead of hanging until a supervisor kills it."""
    done = subprocess.run(
        [sys.executable, "-c", _STOPPED_GATEWAY, pace],
        capture_output=True, text=True, timeout=20,
    )

    assert done.returncode == 0
    assert done.stdout.splitlines() == ["stopped"]


def test_a_stopped_gateway_with_no_run_exits_normally():
    """Nothing unfinished: the exit stays the interpreter's own, atexit hooks
    included, even with an idle pool thread left from a finished run."""
    script = _STOPPED_GATEWAY.replace("threading.Event().wait()", "pass").replace(
        "asyncio.run(_main())", "asyncio.run(_main())\nimport time; time.sleep(0.2)")
    done = subprocess.run(
        [sys.executable, "-c", script, "unpaced"],
        capture_output=True, text=True, timeout=20,
    )

    assert done.returncode == 0
    assert done.stdout.splitlines() == ["stopped", "exited on its own"]


class _ExitedError(Exception):
    pass


def _left_with_a_run_thread(monkeypatch, end_scripts_calls, **kwargs) -> int:
    """Run the real ``leave_unfinished_runs`` with a run thread alive and
    ``os._exit`` replaced by an exception; returns the exit code it asked for.
    ``end_scripts_calls`` (the fixture) also gets ``"exit"`` at the exit."""
    codes: list[int] = []

    def _exit(code):
        codes.append(code)
        end_scripts_calls.append("exit")
        raise _ExitedError

    monkeypatch.setattr(run_threads.os, "_exit", _exit)
    release = threading.Event()
    thread = threading.Thread(target=release.wait, args=(10,), name="workflow-run-left1")
    thread.start()
    try:
        with pytest.raises(_ExitedError):
            _REAL_LEAVE_UNFINISHED_RUNS(**kwargs)
    finally:
        release.set()
        thread.join(5)
    return codes[0]


def test_a_quit_tells_the_person_how_many_runs_were_left(monkeypatch, capsys, end_scripts_calls):
    assert _left_with_a_run_thread(monkeypatch, end_scripts_calls, tell_user=True) == 0
    lines = capsys.readouterr().err.splitlines()
    told = [line for line in lines if "left1" in line]
    assert len(told) == 1
    assert "1 workflow run" in told[0] and "resume" in told[0]
    assert "resume_run_id" in told[0] and "dashboard" in told[0]


def test_a_gateway_stop_keeps_only_its_log_line(monkeypatch, capsys, end_scripts_calls):
    _left_with_a_run_thread(monkeypatch, end_scripts_calls)
    assert "resume_run_id" not in capsys.readouterr().err


def test_the_scripts_of_the_runs_end_right_before_the_exit(monkeypatch, end_scripts_calls):
    assert _left_with_a_run_thread(monkeypatch, end_scripts_calls) == 0
    assert end_scripts_calls == ["end_scripts", "exit"]


def test_a_way_out_on_an_error_exits_with_the_code_it_is_given(monkeypatch, end_scripts_calls):
    """A process leaving its runs because something failed must not exit 0:
    the code it is given is the code it exits with."""
    assert _left_with_a_run_thread(monkeypatch, end_scripts_calls, code=1) == 1
    assert end_scripts_calls == ["end_scripts", "exit"]


def test_with_no_run_left_no_script_is_ended(monkeypatch, end_scripts_calls):
    _REAL_LEAVE_UNFINISHED_RUNS()
    assert end_scripts_calls == []


def test_with_no_run_left_a_code_does_not_end_the_process(monkeypatch, end_scripts_calls):
    """No run on a thread: the caller's own way out (an exception propagating,
    with its traceback and exit code) stays the one taken."""
    codes: list[int] = []
    monkeypatch.setattr(run_threads.os, "_exit", codes.append)
    _REAL_LEAVE_UNFINISHED_RUNS(code=1)
    assert codes == []
    assert end_scripts_calls == []


@pytest.mark.parametrize("where", ["log line", "flush"])
def test_an_interrupted_way_out_still_ends_the_scripts_and_then_exits(
        monkeypatch, end_scripts_calls, where):
    """A second Ctrl+C while the log line is written or the log is flushed
    must not skip the end of the scripts, which would outlive a graceful
    stop, nor the exit."""
    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    if where == "log line":
        monkeypatch.setattr(run_threads.logger, "warning", interrupted)
    else:
        monkeypatch.setattr(run_threads, "_flush_logs_before_exit", interrupted)
    assert _left_with_a_run_thread(monkeypatch, end_scripts_calls) == 0
    assert end_scripts_calls == ["end_scripts", "exit"]


def test_a_failing_end_of_the_scripts_still_exits(monkeypatch):
    from durin.workflow import script_runner

    def broken() -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(script_runner, "end_scripts_for_exit", broken)
    codes: list[int] = []
    monkeypatch.setattr(run_threads.os, "_exit", codes.append)
    release = threading.Event()
    thread = threading.Thread(target=release.wait, args=(10,), name="workflow-run-left2")
    thread.start()
    try:
        with pytest.raises(RuntimeError):
            _REAL_LEAVE_UNFINISHED_RUNS()
    finally:
        release.set()
        thread.join(5)
    assert codes == [0]


def test_the_shutdown_line_says_how_many_scripts_are_ended(monkeypatch, end_scripts_calls):
    from durin.workflow import script_runner

    held = subprocess.Popen(["true"])
    held.wait(timeout=10)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING",
                      filter="durin.workflow.run_threads", format="{message}")
    with script_runner._children_lock:
        script_runner._children.add(held)
    try:
        _left_with_a_run_thread(monkeypatch, end_scripts_calls)
    finally:
        logger.remove(sink)
        with script_runner._children_lock:
            script_runner._children.discard(held)
    [line] = lines
    assert "1 workflow run(s) unfinished" in line
    assert "ending the 1 workflow script(s) this process still holds" in line
