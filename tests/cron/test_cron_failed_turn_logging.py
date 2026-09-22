"""A cron turn that failed at the model is a warning, not a crash dump.

``CronTurnFailedError`` is how the handler reports a turn whose model call
failed (the reply text is the reason). It is an expected outcome, recorded
in the run history; logging it with ``logger.exception`` printed a full
traceback with locals on every such run, which buried the one useful line.
"""

from __future__ import annotations

import time

import pytest
from loguru import logger

from durin.cron.outcome import CronTurnFailedError
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
async def test_a_failed_turn_is_logged_without_a_traceback(tmp_path) -> None:
    async def on_job(job):
        raise CronTurnFailedError("error", "Error calling LLM: Connection error.")

    records: list = []
    sink_id = logger.add(lambda m: records.append(m.record), level="WARNING")
    try:
        service = _service(tmp_path, on_job)
        _due(service, "reminder")
        await service._on_timer()
        await service.wait_for_jobs()
    finally:
        logger.remove(sink_id)

    failed = [r for r in records if "reminder" in r["message"] and "failed" in r["message"]]
    assert len(failed) == 1
    assert failed[0]["level"].name == "WARNING"
    assert failed[0]["exception"] is None
    assert "Connection error" in failed[0]["message"]

    record = CronService(service.store_path).list_jobs(include_disabled=True)[0].state.run_history[-1]
    assert record.status == "error" and "Connection error" in (record.error or "")


@pytest.mark.asyncio
async def test_an_unexpected_exception_keeps_its_traceback(tmp_path) -> None:
    async def on_job(job):
        raise RuntimeError("bug in the handler")

    records: list = []
    sink_id = logger.add(lambda m: records.append(m.record), level="WARNING")
    try:
        service = _service(tmp_path, on_job)
        _due(service, "reminder")
        await service._on_timer()
        await service.wait_for_jobs()
    finally:
        logger.remove(sink_id)

    failed = [r for r in records if "reminder" in r["message"] and "failed" in r["message"]]
    assert len(failed) == 1
    assert failed[0]["level"].name == "ERROR"
    assert failed[0]["exception"] is not None
