"""Regressions for bugs found during live testing of the cron feature.

1. run_job must release self._lock before executing (a long job otherwise
   blocks concurrent cron-store readers and trips filelock's cross-instance
   deadlock guard — the live HTTP 500 when running the daily dream).
2. _job_to_dict must expose run_history for SYSTEM jobs too (so the dream's
   runs are visible in the webui).
3. The cron tool must pass mode/model through to the job.
"""

from __future__ import annotations

import pytest

from durin.agent.tools.context import RequestContext
from durin.agent.tools.cron import CronTool
from durin.cron.service import CronService
from durin.cron.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronRunRecord,
    CronSchedule,
)
from durin.service.cron import _job_to_dict


@pytest.mark.asyncio
async def test_run_job_releases_lock_during_execution(tmp_path):
    """A concurrent reader (second CronService on the same store) must be able
    to load the store WHILE run_job is mid-execution. The old code held
    self._lock across _execute_job, so this deadlocked."""
    store = tmp_path / "cron" / "jobs.json"
    service = CronService(store)
    job = service.add_job(
        name="job",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="x",
    )

    observed: dict = {}

    async def on_job(_j):
        # Mirrors the HTTP `list` endpoint's _fresh_cron_scheduler reading the
        # store while a run is in flight — a different FileLock instance on the
        # same path. Must not deadlock.
        other = CronService(store)
        observed["jobs"] = other.list_jobs(include_disabled=True)

    service.on_job = on_job

    ok = await service.run_job(job.id, force=True)

    assert ok is True
    assert observed.get("jobs"), "concurrent read during execution must succeed"
    # The run was recorded onto the persisted store.
    reloaded = CronService(store).get_job(job.id)
    assert len(reloaded.state.run_history) == 1


def test_job_to_dict_includes_run_history_for_system_jobs():
    """System jobs (the memory dream) must expose run_history so their runs
    show up in the webui — the old code hard-coded [] for is_system."""
    job = CronJob(
        id="memory_dream",
        name="memory_dream",
        schedule=CronSchedule(kind="cron", expr="0 3 * * *"),
        payload=CronPayload(kind="system_event"),
        state=CronJobState(
            run_history=[
                CronRunRecord(run_at_ms=1, status="ok", duration_ms=5),
                CronRunRecord(run_at_ms=2, status="error", duration_ms=7, error="boom"),
            ]
        ),
    )

    d = _job_to_dict(job)

    assert d["is_system"] is True
    assert len(d["run_history"]) == 2
    assert d["run_history"][0]["status"] == "ok"
    assert d["run_history"][1]["error"] == "boom"


def _tool(tmp_path) -> CronTool:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service, default_timezone="UTC")
    tool.set_context(RequestContext(
        channel="cli", chat_id="d", session_key="cli:d", metadata={},
    ))
    return tool


@pytest.mark.asyncio
async def test_tool_add_passes_mode_and_model(tmp_path):
    tool = _tool(tmp_path)

    out = await tool.execute(
        action="add", message="do x", every_seconds=60, mode="task", model="m1",
    )

    assert "Created job" in out
    jobs = tool._cron.list_jobs(include_disabled=True)
    assert jobs[0].payload.mode == "task"
    assert jobs[0].payload.model == "m1"


@pytest.mark.asyncio
async def test_tool_add_defaults_mode_reminder(tmp_path):
    tool = _tool(tmp_path)

    await tool.execute(action="add", message="ping", every_seconds=60)

    jobs = tool._cron.list_jobs(include_disabled=True)
    assert jobs[0].payload.mode == "reminder"
    assert jobs[0].payload.model is None


@pytest.mark.asyncio
async def test_tool_update_changes_mode(tmp_path):
    tool = _tool(tmp_path)
    job = tool._cron.add_job(
        name="j", schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hi", channel="cli", to="d", session_key="cli:d",
    )

    out = await tool.execute(action="update", job_id=job.id, mode="task")

    assert "Error" not in out
    jobs = tool._cron.list_jobs(include_disabled=True)
    assert jobs[0].payload.mode == "task"
