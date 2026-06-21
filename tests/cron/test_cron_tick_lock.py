"""Cross-process test: non-blocking tick lock prevents overlapping scheduler ticks.

A subprocess holds `.tick.lock` while a second process calls `_on_timer` (via
a real asyncio event loop).  Without the tick lock the second process would
also attempt to run any due jobs; with it, it skips the tick immediately and
returns without running any jobs.

After the holder releases the lock, a third call to `_on_timer` MUST run the
due job — verifying the lock does not permanently suppress execution.
"""

import asyncio
import json
import multiprocessing as mp
import os
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------

def _hold_tick_lock(home: str, cron_dir: str, ready_path: str, release_path: str) -> None:
    """Acquire .tick.lock, signal readiness, hold until told to release."""
    os.environ["DURIN_HOME"] = home
    from filelock import FileLock

    tick_lock = FileLock(str(Path(cron_dir) / ".tick.lock"))
    with tick_lock:
        Path(ready_path).touch()
        # Wait until the main process signals release
        deadline = time.monotonic() + 15.0
        while not Path(release_path).exists():
            time.sleep(0.02)
            if time.monotonic() > deadline:
                break


def _call_on_timer(home: str, jobs_dir: str, result_path: str) -> None:
    """Call _on_timer once in a fresh event loop; write job-executed count to result."""
    os.environ["DURIN_HOME"] = home
    from durin.cron.service import CronService

    store_path = Path(jobs_dir) / "jobs.json"
    executed: list[str] = []

    async def fake_on_job(job):
        executed.append(job.id)

    svc = CronService(store_path, on_job=fake_on_job)
    svc._running = True
    svc._arm_timer = lambda: None  # no persistent event loop

    async def run():
        await svc._on_timer()

    asyncio.run(run())
    Path(result_path).write_text(json.dumps(executed))


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def _make_due_job(jobs_dir: Path) -> str:
    """Write a jobs.json with one job already due (next_run_at_ms in the past)."""
    import uuid

    job_id = str(uuid.uuid4())[:8]
    store = {
        "version": 1,
        "jobs": [
            {
                "id": job_id,
                "name": "test-job",
                "enabled": True,
                "schedule": {"kind": "every", "everyMs": 3_600_000, "atMs": None, "expr": None, "tz": None},
                "payload": {
                    "kind": "agent_turn",
                    "message": "tick test",
                    "deliver": False,
                    "channel": None,
                    "to": None,
                    "channelMeta": {},
                    "sessionKey": None,
                },
                "state": {
                    "nextRunAtMs": int(time.time() * 1000) - 5_000,  # 5 s ago
                    "lastRunAtMs": None,
                    "lastStatus": None,
                    "lastError": None,
                    "runHistory": [],
                },
                "createdAtMs": int(time.time() * 1000),
                "updatedAtMs": int(time.time() * 1000),
                "deleteAfterRun": False,
            }
        ],
    }
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "jobs.json").write_text(json.dumps(store))
    return job_id


def test_tick_lock_blocks_second_scheduler(tmp_path: Path) -> None:
    """While .tick.lock is held, _on_timer must return without executing any job.

    After the holder releases the lock, a fresh _on_timer call MUST run the
    due job.
    """
    home = str(tmp_path / "home")
    cron_dir = str(tmp_path / "home" / "cron")
    ready_path = str(tmp_path / "ready.sentinel")
    release_path = str(tmp_path / "release.sentinel")
    result_path_blocked = str(tmp_path / "result_blocked.json")
    result_path_after = str(tmp_path / "result_after.json")

    # Set up a due job
    _make_due_job(Path(cron_dir))

    ctx = mp.get_context("spawn")

    # Start the lock-holder subprocess
    holder = ctx.Process(
        target=_hold_tick_lock,
        args=(home, cron_dir, ready_path, release_path),
    )
    holder.start()

    # Wait for holder to acquire the lock
    deadline = time.monotonic() + 10.0
    while not Path(ready_path).exists():
        time.sleep(0.05)
        assert time.monotonic() < deadline, "holder never acquired tick lock"

    # Call _on_timer from a second subprocess — lock is held, should skip
    second = ctx.Process(
        target=_call_on_timer,
        args=(home, cron_dir, result_path_blocked),
    )
    second.start()
    second.join(10)
    assert second.exitcode == 0, f"second process exited with code {second.exitcode}"

    executed_blocked = json.loads(Path(result_path_blocked).read_text())
    assert executed_blocked == [], (
        f"Expected zero jobs while tick lock held; got {executed_blocked}. "
        "The tick lock is missing or not non-blocking."
    )

    # Release the holder
    Path(release_path).touch()
    holder.join(10)
    assert holder.exitcode == 0, f"holder exited with code {holder.exitcode}"

    # Now call _on_timer again — lock is free, job MUST execute
    after = ctx.Process(
        target=_call_on_timer,
        args=(home, cron_dir, result_path_after),
    )
    after.start()
    after.join(10)
    assert after.exitcode == 0, f"after process exited with code {after.exitcode}"

    executed_after = json.loads(Path(result_path_after).read_text())
    assert len(executed_after) == 1, (
        f"Expected exactly 1 job after lock released; got {executed_after}. "
        "Job was not run after the tick lock freed."
    )
