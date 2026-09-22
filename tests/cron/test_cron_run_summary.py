"""A run record carries what the job said.

``CronRunRecord.summary`` was declared and never written: the job callback
returns the turn's reply and the scheduler threw it away, so the history
showed when a job ran and whether it failed, never what it produced.
"""

from __future__ import annotations

import time

import pytest

from durin.cron.service import CronService
from durin.cron.types import CronSchedule


def _service(tmp_path, on_job) -> CronService:
    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job)
    service._running = True
    service._load_store()
    service._arm_timer = lambda: None
    return service


def _due(service: CronService, name: str):
    job = service.add_job(name=name, schedule=CronSchedule(kind="every", every_ms=3_600_000), message="tick")
    job.state.next_run_at_ms = int(time.time() * 1000) - 1_000
    service._save_store()
    return job


@pytest.mark.asyncio
async def test_the_run_record_keeps_the_reply_as_its_summary(tmp_path) -> None:
    async def on_job(job):
        return "Three invoices are overdue; the oldest is from March."

    service = _service(tmp_path, on_job)
    _due(service, "invoices")
    await service._on_timer()
    await service.wait_for_jobs()

    persisted = CronService(service.store_path).list_jobs(include_disabled=True)[0]
    record = persisted.state.run_history[-1]
    assert record.status == "ok"
    assert record.summary == "Three invoices are overdue; the oldest is from March."


@pytest.mark.asyncio
async def test_a_long_reply_is_cut_to_a_summary(tmp_path) -> None:
    async def on_job(job):
        return "x" * 2_000

    service = _service(tmp_path, on_job)
    _due(service, "long")
    await service._on_timer()
    await service.wait_for_jobs()

    record = CronService(service.store_path).list_jobs(include_disabled=True)[0].state.run_history[-1]
    assert record.summary is not None
    assert len(record.summary) <= 500


@pytest.mark.asyncio
async def test_a_silent_job_has_no_summary(tmp_path) -> None:
    async def on_job(job):
        return None

    service = _service(tmp_path, on_job)
    _due(service, "silent")
    await service._on_timer()
    await service.wait_for_jobs()

    record = CronService(service.store_path).list_jobs(include_disabled=True)[0].state.run_history[-1]
    assert record.status == "ok" and record.summary is None
