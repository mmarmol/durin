"""The log says whether a cron run came from its schedule or by hand.

A dream run outside its schedule logged exactly like the scheduled one
("Cron: executing job …"), so the gateway log could not tell a run-now from
the dashboard or the CLI from the 03:00 run.
"""

from __future__ import annotations

import time

import pytest
from loguru import logger

from durin.cron.service import CronService
from durin.cron.types import CronSchedule


def _service(tmp_path, on_job) -> CronService:
    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job)
    service._running = True
    service._load_store()
    service._arm_timer = lambda: None
    return service


@pytest.mark.asyncio
async def test_the_log_tells_a_scheduled_run_from_one_run_by_hand(tmp_path) -> None:
    async def on_job(job):
        return "done"

    records: list = []
    sink_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        service = _service(tmp_path, on_job)
        job = service.add_job(name="nightly", schedule=CronSchedule(kind="every", every_ms=3_600_000),
                              message="tick")
        job.state.next_run_at_ms = int(time.time() * 1000) - 1_000
        service._save_store()
        await service._on_timer()
        await service.wait_for_jobs()
        assert await service.run_job(job.id)
    finally:
        logger.remove(sink_id)

    starts = [r["message"] for r in records if "executing job 'nightly'" in r["message"]]
    assert len(starts) == 2
    assert starts[0].endswith("on schedule")
    assert starts[1].endswith("run by hand")
