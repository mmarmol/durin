"""A schedule change made through the API reaches the running scheduler now.

The API mutates the schedule through a fresh, non-running ``CronService``
that appends to the offline action log. The live scheduler merges that log
at its next timer tick, which with nothing due is five minutes away — so a
one-shot created for thirty seconds from now fired minutes late. After every
write the API now wakes the live scheduler it holds a handle to.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from durin.service.cron import (
    CronAddCommand,
    CronRemoveCommand,
    CronService,
    CronToggleCommand,
    CronUpdateCommand,
)
from durin.service.principal import Principal


def _seed(tmp_path: Path) -> Path:
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(json.dumps({
        "version": 1,
        "jobs": [{
            "id": "abc12345",
            "name": "test job",
            "enabled": True,
            "schedule": {"kind": "every", "everyMs": 3600000, "atMs": None, "expr": None, "tz": None},
            "payload": {
                "kind": "agent_turn", "message": "hello", "deliver": False,
                "channel": None, "to": None, "channelMeta": {}, "sessionKey": None,
            },
            "state": {"nextRunAtMs": None, "lastRunAtMs": None, "lastStatus": None, "lastError": None, "runHistory": []},
            "createdAtMs": 1000000,
            "updatedAtMs": 1000000,
            "deleteAfterRun": False,
        }],
    }), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = _seed(tmp_path)
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: SimpleNamespace(workspace_path=ws))
    return ws


def _live() -> MagicMock:
    live = MagicMock()
    live.wake = MagicMock()
    return live


async def test_create_wakes_the_live_scheduler(workspace: Path) -> None:
    live = _live()
    await CronService(cron_scheduler=live).create(
        CronAddCommand(name="soon", schedule_kind="every", every_ms=60_000, message="ping"),
        Principal.local(),
    )
    live.wake.assert_called_once_with()


async def test_update_toggle_and_remove_wake_the_live_scheduler(workspace: Path) -> None:
    live = _live()
    api = CronService(cron_scheduler=live)
    await api.update(CronUpdateCommand(id="abc12345", message="changed"), Principal.local())
    await api.toggle(CronToggleCommand(id="abc12345", enabled=False), Principal.local())
    await api.remove(CronRemoveCommand(id="abc12345"), Principal.local())
    assert live.wake.call_count == 3


async def test_writes_still_work_with_no_live_scheduler(workspace: Path) -> None:
    result = await CronService().create(
        CronAddCommand(name="soon", schedule_kind="every", every_ms=60_000, message="ping"),
        Principal.local(),
    )
    assert result.job.name == "soon"
