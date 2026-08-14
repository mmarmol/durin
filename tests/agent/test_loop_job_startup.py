"""Gateway startup: reconciling orphaned jobs and picking up queued ones.

``AgentLoop.run()`` resumes OCR work before doing anything else. Rows
``reconcile`` finds orphaned (a dead or long-stale worker pid) get a fresh
worker via ``respawn``; rows left ``queued`` with nothing ever having
claimed them -- a spawn whose own ``Popen`` failed, or a cap-refused
worker's chain-launcher crashing -- get one too, capped at
``MAX_CONCURRENT_OCR_JOBS``. Both loops only ever launch a worker process;
neither claims a job itself (that stays exclusively the worker's own call,
see ``tests/jobs/test_ocr_worker.py``).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

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
    integer (mirrors tests/jobs/test_spawn.py's own ``_pid_alive`` fixture)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


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
