"""SP1: CronService — unit tests (called directly, no HTTP)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.service.cron import (
    CronAddCommand,
    CronListQuery,
    CronRemoveCommand,
    CronRunCommand,
    CronService,
    CronToggleCommand,
    CronUpdateCommand,
)
from durin.service.principal import Principal, Scope
from durin.service.types import (
    ForbiddenError,
    NotFoundError,
    UnavailableError,
    ValidationFailedError,
)


def _seed_jobs(tmp_path: Path) -> Path:
    """Write jobs.json (camelCase, as the CronScheduler expects) and return its path."""
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    jobs_data = {
        "version": 1,
        "jobs": [
            {
                "id": "abc12345",
                "name": "test job",
                "enabled": True,
                "schedule": {
                    "kind": "every",
                    "everyMs": 3600000,
                    "atMs": None,
                    "expr": None,
                    "tz": None,
                },
                "payload": {
                    "kind": "agent_turn",
                    "message": "hello",
                    "deliver": False,
                    "channel": None,
                    "to": None,
                    "channelMeta": {},
                    "sessionKey": None,
                },
                "state": {
                    "nextRunAtMs": None,
                    "lastRunAtMs": None,
                    "lastStatus": None,
                    "lastError": None,
                    "runHistory": [],
                },
                "createdAtMs": 1000000,
                "updatedAtMs": 1000000,
                "deleteAfterRun": False,
            },
            {
                "id": "sys00001",
                "name": "system consolidation",
                "enabled": True,
                "schedule": {
                    "kind": "cron",
                    "expr": "0 3 * * *",
                    "tz": "UTC",
                    "atMs": None,
                    "everyMs": None,
                },
                "payload": {
                    "kind": "system_event",
                    "message": "__consolidate__",
                    "deliver": False,
                    "channel": None,
                    "to": None,
                    "channelMeta": {},
                    "sessionKey": None,
                },
                "state": {
                    "nextRunAtMs": None,
                    "lastRunAtMs": None,
                    "lastStatus": None,
                    "lastError": None,
                    "runHistory": [],
                },
                "createdAtMs": 1000000,
                "updatedAtMs": 1000000,
                "deleteAfterRun": False,
            },
        ],
    }
    jobs_path = cron_dir / "jobs.json"
    jobs_path.write_text(json.dumps(jobs_data), encoding="utf-8")
    return cron_dir.parent  # workspace root


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a workspace with two cron jobs and patch load_config to use it."""
    ws = _seed_jobs(tmp_path)
    fake_cfg = SimpleNamespace(workspace_path=ws)
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: fake_cfg)
    return ws


async def test_list_returns_all_jobs(workspace: Path) -> None:
    result = await CronService().list(CronListQuery(), Principal.local())
    assert len(result.jobs) == 2
    ids = {j.id for j in result.jobs}
    assert "abc12345" in ids
    assert "sys00001" in ids


async def test_list_job_shape(workspace: Path) -> None:
    result = await CronService().list(CronListQuery(), Principal.local())
    user_job = next(j for j in result.jobs if j.id == "abc12345")

    assert user_job.name == "test job"
    assert user_job.enabled is True
    assert user_job.is_system is False
    assert user_job.message == "hello"
    assert user_job.schedule.kind == "every"
    assert user_job.schedule.label == "every 1h"
    assert user_job.schedule.every_ms == 3600000

    sys_job = next(j for j in result.jobs if j.id == "sys00001")
    assert sys_job.is_system is True
    assert sys_job.message == ""  # hidden for system jobs
    assert sys_job.schedule.kind == "cron"
    assert "0 3 * * *" in sys_job.schedule.label
    assert "(UTC)" in sys_job.schedule.label


async def test_list_requires_read_scope(workspace: Path) -> None:
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await CronService().list(CronListQuery(), principal)


async def test_remove_user_job(workspace: Path) -> None:
    result = await CronService().remove(
        CronRemoveCommand(id="abc12345"), Principal.local()
    )
    assert result.result == "removed"

    listed = await CronService().list(CronListQuery(), Principal.local())
    assert not any(j.id == "abc12345" for j in listed.jobs)


async def test_remove_unknown_raises_not_found(workspace: Path) -> None:
    with pytest.raises(NotFoundError):
        await CronService().remove(CronRemoveCommand(id="ghost"), Principal.local())


async def test_remove_system_job_raises_forbidden(workspace: Path) -> None:
    with pytest.raises(ForbiddenError):
        await CronService().remove(CronRemoveCommand(id="sys00001"), Principal.local())


async def test_remove_requires_write_scope(workspace: Path) -> None:
    principal = Principal.remote("t", frozenset({Scope.CRON_READ.value}))
    with pytest.raises(ForbiddenError):
        await CronService().remove(CronRemoveCommand(id="abc12345"), principal)


async def test_toggle_disables_job(workspace: Path) -> None:
    result = await CronService().toggle(
        CronToggleCommand(id="abc12345", enabled=False), Principal.local()
    )
    assert result.job.id == "abc12345"
    assert result.job.enabled is False


async def test_toggle_unknown_raises_not_found(workspace: Path) -> None:
    with pytest.raises(NotFoundError):
        await CronService().toggle(
            CronToggleCommand(id="ghost", enabled=True), Principal.local()
        )


async def test_toggle_requires_write_scope(workspace: Path) -> None:
    principal = Principal.remote("t", frozenset({Scope.CRON_READ.value}))
    with pytest.raises(ForbiddenError):
        await CronService().toggle(
            CronToggleCommand(id="abc12345", enabled=False), principal
        )


async def test_run_raises_unavailable_when_no_scheduler(workspace: Path) -> None:
    with pytest.raises(UnavailableError):
        await CronService(cron_scheduler=None).run(
            CronRunCommand(id="abc12345"), Principal.local()
        )


async def test_run_raises_not_found_for_unknown_job(workspace: Path) -> None:
    mock_scheduler = MagicMock()
    mock_scheduler.get_job.return_value = None
    with pytest.raises(NotFoundError):
        await CronService(cron_scheduler=mock_scheduler).run(
            CronRunCommand(id="ghost"), Principal.local()
        )


async def test_run_returns_started_false_when_executing(workspace: Path) -> None:
    mock_scheduler = MagicMock()
    mock_scheduler.get_job.return_value = MagicMock()
    mock_scheduler.is_executing.return_value = True

    result = await CronService(cron_scheduler=mock_scheduler).run(
        CronRunCommand(id="abc12345"), Principal.local()
    )
    assert result.started is False
    assert result.reason == "already_running"


async def test_run_returns_started_true_when_ready(workspace: Path) -> None:
    mock_scheduler = MagicMock()
    mock_scheduler.get_job.return_value = MagicMock()
    mock_scheduler.is_executing.return_value = False
    mock_scheduler.run_job = AsyncMock()

    result = await CronService(cron_scheduler=mock_scheduler).run(
        CronRunCommand(id="abc12345"), Principal.local()
    )
    assert result.started is True
    assert result.reason is None


async def test_run_requires_write_scope(workspace: Path) -> None:
    mock_scheduler = MagicMock()
    principal = Principal.remote("t", frozenset({Scope.CRON_READ.value}))
    with pytest.raises(ForbiddenError):
        await CronService(cron_scheduler=mock_scheduler).run(
            CronRunCommand(id="abc12345"), principal
        )


async def test_run_now_spawns_run_job(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bool]] = []

    class FakeScheduler:
        def get_job(self, jid: str) -> SimpleNamespace:
            return SimpleNamespace(id=jid)

        def is_executing(self, jid: str) -> bool:
            return False

        async def run_job(self, jid: str, force: bool = False) -> None:
            calls.append((jid, force))

    svc = CronService(cron_scheduler=FakeScheduler())
    res = await svc.run(CronRunCommand(id="abc"), Principal.local())
    await asyncio.sleep(0.01)
    assert res.started is True
    assert calls == [("abc", True)]


async def test_run_now_skips_spawn_when_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bool]] = []

    class FakeScheduler:
        def get_job(self, jid: str) -> SimpleNamespace:
            return SimpleNamespace(id=jid)

        def is_executing(self, jid: str) -> bool:
            return True

        async def run_job(self, jid: str, force: bool = False) -> None:
            calls.append((jid, force))

    svc = CronService(cron_scheduler=FakeScheduler())
    res = await svc.run(CronRunCommand(id="abc"), Principal.local())
    await asyncio.sleep(0.01)
    assert res.started is False
    assert res.reason == "already_running"
    assert calls == []


async def test_create_job(workspace: Path) -> None:
    cmd = CronAddCommand(
        name="nightly",
        mode="task",
        message="run X",
        schedule_kind="cron",
        expr="0 3 * * *",
        deliver=False,
        model="m1",
    )
    res = await CronService().create(cmd, Principal.local())
    assert res.job.name == "nightly"
    listed = await CronService().list(CronListQuery(), Principal.local())
    assert any(j.name == "nightly" for j in listed.jobs)


async def test_create_interval_job_has_next_run(workspace: Path) -> None:
    """An 'every' job created via the API must get a non-None next_run_at_ms.

    Regression for the form/backend schedule-kind vocabulary mismatch
    (the form sent "interval", which fell through _compute_next_run → None,
    so the job was created but never fired). The backend vocabulary is
    "every"; a job created with it must be schedulable.
    """
    cmd = CronAddCommand(
        name="interval-job",
        message="ping",
        schedule_kind="every",
        every_ms=3_600_000,
    )
    res = await CronService().create(cmd, Principal.local())
    assert res.job.schedule.kind == "every"
    assert res.job.schedule.every_ms == 3_600_000
    assert res.job.state.next_run_at_ms is not None


async def test_create_rejects_invalid_schedule_kind(workspace: Path) -> None:
    cmd = CronAddCommand(
        name="bad-kind",
        message="ping",
        schedule_kind="interval",
        every_ms=3_600_000,
    )
    with pytest.raises(ValidationFailedError):
        await CronService().create(cmd, Principal.local())


async def test_update_rejects_invalid_schedule_kind(workspace: Path) -> None:
    cmd = CronUpdateCommand(
        id="abc12345",
        schedule_kind="interval",
        every_ms=3_600_000,
    )
    with pytest.raises(ValidationFailedError):
        await CronService().update(cmd, Principal.local())


async def test_create_job_carries_mode_and_model(workspace: Path) -> None:
    """CronJobItem must expose mode + model so the edit form can restore them."""
    cmd = CronAddCommand(
        name="with-meta",
        mode="task",
        message="run X",
        schedule_kind="cron",
        expr="0 3 * * *",
        model="zai/glm-5",
    )
    res = await CronService().create(cmd, Principal.local())
    assert res.job.mode == "task"
    assert res.job.model == "zai/glm-5"

    listed = await CronService().list(CronListQuery(), Principal.local())
    created = next(j for j in listed.jobs if j.name == "with-meta")
    assert created.mode == "task"
    assert created.model == "zai/glm-5"


async def test_create_and_update_job_carry_persona(workspace: Path) -> None:
    """A job can run as a persona; create stores it, update changes it, and the
    item exposes it so the edit form can restore the choice."""
    res = await CronService().create(
        CronAddCommand(
            name="with-persona",
            mode="task",
            message="run X",
            schedule_kind="cron",
            expr="0 3 * * *",
            persona="researcher",
        ),
        Principal.local(),
    )
    assert res.job.persona == "researcher"
    assert res.job.model is None

    job_id = res.job.id
    upd = await CronService().update(
        CronUpdateCommand(id=job_id, persona="engineer"), Principal.local()
    )
    assert upd.job.persona == "engineer"

    listed = await CronService().list(CronListQuery(), Principal.local())
    created = next(j for j in listed.jobs if j.id == job_id)
    assert created.persona == "engineer"


async def test_list_job_mode_model_defaults(workspace: Path) -> None:
    result = await CronService().list(CronListQuery(), Principal.local())
    user_job = next(j for j in result.jobs if j.id == "abc12345")
    assert user_job.mode == "reminder"
    assert user_job.model is None


async def test_create_job_run_history_empty(workspace: Path) -> None:
    cmd = CronAddCommand(
        name="history-test",
        message="hello",
        schedule_kind="every",
        every_ms=3600000,
    )
    res = await CronService().create(cmd, Principal.local())
    assert res.job.run_history == []


async def test_list_job_run_history_field(workspace: Path) -> None:
    result = await CronService().list(CronListQuery(), Principal.local())
    user_job = next(j for j in result.jobs if j.id == "abc12345")
    assert hasattr(user_job, "run_history")
    assert user_job.run_history == []


async def test_update_rejects_system_job(workspace: Path) -> None:
    with pytest.raises(ForbiddenError):
        await CronService().update(
            CronUpdateCommand(id="sys00001", message="x"), Principal.local()
        )


async def test_update_rejects_unknown_job(workspace: Path) -> None:
    with pytest.raises(NotFoundError):
        await CronService().update(
            CronUpdateCommand(id="ghost999", message="x"), Principal.local()
        )


async def test_update_user_job(workspace: Path) -> None:
    cmd = CronUpdateCommand(id="abc12345", name="renamed", message="updated msg")
    res = await CronService().update(cmd, Principal.local())
    assert res.job.id == "abc12345"
    assert res.job.name == "renamed"
    assert res.job.message == "updated msg"
