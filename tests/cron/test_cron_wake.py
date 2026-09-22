"""A running scheduler can be woken so a change written through the offline
action log is picked up now, not at the next timer tick.

The webui and the API mutate the schedule through a non-running
``CronService`` that appends to ``action.jsonl``; the live scheduler merges
that log only when its timer fires, which with nothing due is
``max_sleep_ms`` (five minutes) away. A one-shot due in thirty seconds
therefore fired minutes late. ``wake()`` re-arms the timer to fire at once.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from durin.cron.service import CronService
from durin.cron.types import CronSchedule


@pytest.mark.asyncio
async def test_wake_makes_the_running_scheduler_merge_an_offline_add_now(tmp_path) -> None:
    store = tmp_path / "cron" / "jobs.json"
    ran: list[str] = []

    async def on_job(job):
        ran.append(job.name)

    running = CronService(store, on_job=on_job, max_sleep_ms=300_000)
    await running.start()
    try:
        # The API's path: a fresh, non-running instance appends the add to
        # the action log; the running scheduler is asleep for five minutes.
        fresh = CronService(store)
        fresh.add_job(
            name="soon",
            schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 200),
            message="now-ish",
        )
        # Due, merged on read, but the timer is still asleep: nothing runs.
        await asyncio.sleep(0.6)
        assert ran == []

        running.wake()

        await asyncio.sleep(1.0)
        await running.wait_for_jobs()
        assert ran == ["soon"]
    finally:
        running.stop()


def test_wake_on_a_stopped_scheduler_is_a_no_op(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    service.wake()
    assert service._timer_task is None
