"""The tick loop never waits on a job: each due job runs in its own task,
bounded by the pool, and a job never overlaps itself."""
import asyncio
import time

import pytest

from durin.cron.service import CronService
from durin.cron.types import CronSchedule


def _service(tmp_path, on_job, **kw) -> CronService:
    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job, **kw)
    service._running = True
    service._load_store()
    service._arm_timer = lambda: None
    return service


def _due(service: CronService, name: str, every_ms: int = 3_600_000):
    job = service.add_job(name=name, schedule=CronSchedule(kind="every", every_ms=every_ms), message="tick")
    job.state.next_run_at_ms = int(time.time() * 1000) - 1_000
    service._save_store()
    return job


@pytest.mark.asyncio
async def test_a_slow_job_does_not_delay_the_other_due_jobs(tmp_path) -> None:
    started, finished = {}, {}

    async def on_job(job):
        started[job.name] = time.perf_counter()
        await asyncio.sleep(0.4 if job.name == "slow" else 0)
        finished[job.name] = time.perf_counter()

    service = _service(tmp_path, on_job)
    slow, quick = _due(service, "slow"), _due(service, "quick")
    t0 = time.perf_counter()
    await service._on_timer()
    tick_ms = (time.perf_counter() - t0) * 1000
    assert tick_ms < 200, f"the tick waited on a job: {tick_ms:.0f} ms"
    await service.wait_for_jobs()
    assert finished["quick"] < finished["slow"]
    assert started["quick"] - started["slow"] < 0.1  # both started right away
    persisted = {j.name: j for j in CronService(service.store_path).list_jobs(include_disabled=True)}
    assert persisted["slow"].state.last_status == "ok" and persisted["quick"].state.last_status == "ok"
    assert len(persisted["slow"].state.run_history) == 1 and len(persisted["quick"].state.run_history) == 1


@pytest.mark.asyncio
async def test_the_pool_bounds_how_many_jobs_run_at_once(tmp_path) -> None:
    running, peak = [0], [0]

    async def on_job(job):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0.05)
        running[0] -= 1

    service = _service(tmp_path, on_job, max_concurrent_jobs=2)
    for i in range(5):
        _due(service, f"job{i}")
    await service._on_timer()
    await service.wait_for_jobs()
    assert peak[0] == 2
    assert all(j.state.last_status == "ok" for j in CronService(service.store_path).list_jobs())


@pytest.mark.asyncio
async def test_a_job_never_overlaps_itself_and_keeps_its_newest_run_state(tmp_path) -> None:
    runs = []

    async def on_job(job):
        runs.append(job.name)
        await asyncio.sleep(0.2)

    service = _service(tmp_path, on_job)
    job = _due(service, "long", every_ms=1)
    await service._on_timer()          # spawns run 1
    await asyncio.sleep(0.05)
    job_again = next(j for j in service._load_store().jobs if j.id == job.id)
    job_again.state.next_run_at_ms = int(time.time() * 1000) - 1
    service._save_store()
    await service._on_timer()          # due again while run 1 is in flight -> skipped
    await service.wait_for_jobs()
    assert runs == ["long"]
    persisted = next(j for j in CronService(service.store_path).list_jobs())
    assert len(persisted.state.run_history) == 1 and persisted.state.last_status == "ok"


@pytest.mark.asyncio
async def test_rearming_the_timer_does_not_cancel_a_running_job(tmp_path) -> None:
    finished = []

    async def on_job(job):
        await asyncio.sleep(0.15)
        finished.append(job.name)

    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job, max_sleep_ms=50)
    await service.start()
    try:
        _due(service, "steady")
        await service._on_timer()
        await asyncio.sleep(0.02)
        # A mutation while the job runs re-arms the timer (cancels the timer
        # task); the job task is separate and must run to completion.
        service.add_job(name="another", schedule=CronSchedule(kind="every", every_ms=3_600_000), message="x")
        await service.wait_for_jobs()
    finally:
        service.stop()
    assert finished == ["steady"]


@pytest.mark.asyncio
async def test_stop_cancels_running_job_tasks(tmp_path) -> None:
    cancelled = []

    async def on_job(job):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(job.name)
            raise

    service = _service(tmp_path, on_job)
    _due(service, "hang")
    await service._on_timer()
    await asyncio.sleep(0.02)
    service.stop()
    await service.wait_for_jobs()
    assert cancelled == ["hang"]
