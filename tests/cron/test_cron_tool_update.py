"""Tests for the cron tool's ``update`` action."""

from __future__ import annotations

import pytest

from durin.agent.tools.context import RequestContext
from durin.agent.tools.cron import CronTool
from durin.cron.service import CronService
from durin.cron.types import CronSchedule


def _setup(tmp_path) -> CronTool:
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service, default_timezone="UTC")
    tool.set_context(RequestContext(
        channel="cli",
        chat_id="d",
        session_key="cli:d",
        metadata={},
    ))
    return tool


def _seed_job(tool: CronTool, *, message="hi", name="job1", every_seconds=60):
    schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
    job = tool._cron.add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=True,
        channel="cli",
        to="d",
        delete_after_run=False,
        session_key="cli:d",
    )
    return job


@pytest.mark.asyncio
async def test_update_renames_job(tmp_path):
    tool = _setup(tmp_path)
    job = _seed_job(tool)

    out = await tool.execute(
        action="update",
        job_id=job.id,
        name="renamed",
    )
    assert "Updated job 'renamed'" in out

    fresh = tool._cron.get_job(job.id)
    assert fresh.name == "renamed"


@pytest.mark.asyncio
async def test_update_changes_message(tmp_path):
    tool = _setup(tmp_path)
    job = _seed_job(tool, message="old text")

    await tool.execute(
        action="update",
        job_id=job.id,
        message="new text",
    )

    fresh = tool._cron.get_job(job.id)
    assert fresh.payload.message == "new text"


@pytest.mark.asyncio
async def test_update_swaps_schedule_every_to_cron(tmp_path):
    tool = _setup(tmp_path)
    job = _seed_job(tool, every_seconds=60)

    out = await tool.execute(
        action="update",
        job_id=job.id,
        cron_expr="0 9 * * *",
        tz="America/Denver",
    )
    assert "cron: 0 9 * * *" in out

    fresh = tool._cron.get_job(job.id)
    assert fresh.schedule.kind == "cron"
    assert fresh.schedule.expr == "0 9 * * *"
    assert fresh.schedule.tz == "America/Denver"


@pytest.mark.asyncio
async def test_update_disables_delivery(tmp_path):
    tool = _setup(tmp_path)
    job = _seed_job(tool)

    out = await tool.execute(
        action="update",
        job_id=job.id,
        deliver=False,
    )
    assert "Updated job" in out

    fresh = tool._cron.get_job(job.id)
    assert fresh.payload.deliver is False


@pytest.mark.asyncio
async def test_update_unknown_job_returns_not_found(tmp_path):
    tool = _setup(tmp_path)
    out = await tool.execute(action="update", job_id="ghost", name="x")
    assert "not found" in out.lower()


@pytest.mark.asyncio
async def test_update_requires_at_least_one_change(tmp_path):
    """Calling update with only job_id (and no actual changes) errors,
    so the model gets clear feedback instead of a silent no-op."""
    tool = _setup(tmp_path)
    job = _seed_job(tool)

    out = await tool.execute(action="update", job_id=job.id)
    assert "Error" in out
    assert "at least one field" in out.lower()


@pytest.mark.asyncio
async def test_update_rejects_multiple_schedule_types(tmp_path):
    tool = _setup(tmp_path)
    job = _seed_job(tool)

    out = await tool.execute(
        action="update",
        job_id=job.id,
        every_seconds=30,
        cron_expr="0 * * * *",
    )
    assert "Error" in out
    assert "at most ONE" in out


@pytest.mark.asyncio
async def test_update_rejects_tz_without_cron_expr(tmp_path):
    tool = _setup(tmp_path)
    job = _seed_job(tool)

    out = await tool.execute(
        action="update",
        job_id=job.id,
        every_seconds=30,
        tz="America/Denver",
    )
    assert "Error" in out
    assert "tz" in out.lower()


@pytest.mark.asyncio
async def test_update_rejects_invalid_iso_datetime(tmp_path):
    tool = _setup(tmp_path)
    job = _seed_job(tool)

    out = await tool.execute(
        action="update",
        job_id=job.id,
        at="not-a-date",
    )
    assert "Error" in out
    assert "ISO datetime" in out


@pytest.mark.asyncio
async def test_update_missing_job_id_returns_error(tmp_path):
    tool = _setup(tmp_path)
    out = await tool.execute(action="update", name="rename")
    assert "Error" in out


def test_update_action_is_enum_listed_in_schema(tmp_path):
    """The schema must advertise 'update' so the model knows it exists."""
    tool = _setup(tmp_path)
    actions = tool.parameters["properties"]["action"]["enum"]
    assert "update" in actions
