"""Execution-window merge test for CronService._on_timer.

Bug: _on_timer releases self._lock before executing jobs, then re-acquires
and calls _save_store() WITHOUT reloading.  Any external write to jobs.json
during the execution window (another process calling add_job / update_job /
remove_job) is clobbered by the post-execution save.

Fix: under the final self._lock, reload from disk FIRST, then re-apply only
the run-state deltas for each executed job onto the freshly-reloaded store,
then _save_store().

This test uses an in-process deterministic simulation: we monkeypatch
_execute_job so that, mid-execution, a second CronService instance (simulating
another process) adds a new job to jobs.json.  After _on_timer returns we
assert BOTH that the externally-added job still exists AND that the executed
job's run-state delta was persisted.

See docs/architecture/concurrency.md.
"""

import asyncio
import time

import pytest

from durin.cron.service import CronService
from durin.cron.types import CronSchedule


@pytest.mark.asyncio
async def test_exec_window_external_add_survives(tmp_path) -> None:
    """External add_job during the execution window must not be clobbered.

    Without the reload-merge fix, the post-execution _save_store() writes back
    the pre-execution snapshot, wiping the externally-added job.
    """
    store_path = tmp_path / "cron" / "jobs.json"

    executed_jobs: list[str] = []
    external_add_done: list[bool] = []

    service = CronService(store_path, on_job=None)
    service._running = True
    service._load_store()
    service._arm_timer = lambda: None

    # Add the primary job, make it due immediately
    job = service.add_job(
        name="primary",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="tick",
    )
    job.state.next_run_at_ms = int(time.time() * 1000) - 1_000
    service._save_store()

    # Patch _execute_job: record execution and, mid-execution, have a SECOND
    # CronService instance add a new job to the same jobs.json.
    original_execute = service._execute_job

    async def _execute_with_external_add(j):
        executed_jobs.append(j.id)
        # Simulate concurrent external writer (second process / second instance)
        external = CronService(store_path)
        external._running = True
        external._arm_timer = lambda: None
        external.add_job(
            name="external-job",
            schedule=CronSchedule(kind="every", every_ms=3_600_000),
            message="from outside",
        )
        external_add_done.append(True)
        # Also call the original to record proper run-state deltas
        await original_execute(j)

    service._execute_job = _execute_with_external_add

    await service._on_timer()

    # --- Assertions ---
    assert executed_jobs == [job.id], "primary job must have executed"
    assert external_add_done, "external add must have fired during execution window"

    # Load fresh from disk to see what was actually persisted
    reader = CronService(store_path)
    all_jobs = reader.list_jobs(include_disabled=True)
    names = {j.name for j in all_jobs}

    # (a) The externally-added job must still exist
    assert "external-job" in names, (
        f"external-job was clobbered by the post-execution save; got names={names!r}. "
        "The reload-merge fix is missing."
    )

    # (b) The executed job's run-state delta must have been persisted
    primary = next(j for j in all_jobs if j.name == "primary")
    assert primary.state.last_run_at_ms is not None, (
        "primary job's last_run_at_ms was not persisted after execution"
    )
    assert primary.state.last_status == "ok", (
        f"primary job's last_status should be 'ok'; got {primary.state.last_status!r}"
    )
    assert len(primary.state.run_history) == 1, (
        f"primary job's run_history should have 1 entry; got {primary.state.run_history!r}"
    )


@pytest.mark.asyncio
async def test_exec_window_externally_removed_job_not_resurrected(tmp_path) -> None:
    """If a job is externally removed during the execution window, the
    post-execution merge must NOT resurrect it.
    """
    store_path = tmp_path / "cron" / "jobs.json"

    service = CronService(store_path, on_job=None)
    service._running = True
    service._load_store()
    service._arm_timer = lambda: None

    job = service.add_job(
        name="ephemeral",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="tick",
    )
    job.state.next_run_at_ms = int(time.time() * 1000) - 1_000
    service._save_store()

    original_execute = service._execute_job

    async def _execute_then_remove(j):
        await original_execute(j)
        # External writer removes the job while execution is still "in flight"
        external = CronService(store_path)
        external._running = True
        external._arm_timer = lambda: None
        external.remove_job(j.id)

    service._execute_job = _execute_then_remove

    await service._on_timer()

    reader = CronService(store_path)
    all_jobs = reader.list_jobs(include_disabled=True)
    ids = {j.id for j in all_jobs}
    assert job.id not in ids, (
        f"externally-removed job {job.id!r} was resurrected by the merge; "
        "the fix must skip jobs absent from the reloaded store."
    )
