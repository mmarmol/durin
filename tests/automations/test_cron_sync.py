"""Ported from tests/loops/test_cron_sync.py (durin.loops.cron_sync is the
reference; durin/loops/ and tests/loops/ are left untouched by this task —
a later gateway-wiring task owns their removal) plus new prune/protection
cases: automation_trigger jobs are protected from the public
remove_job/update_job API (unlike loop_trigger), so sync_all's removal
paths must use the bypass door instead."""

from pathlib import Path

from durin.automations.cron_sync import (
    automation_job_id,
    remove_automation_jobs,
    sync_all,
    sync_automation_jobs,
)
from durin.automations.spec import parse_automation
from durin.automations.store import save_automation
from durin.cron.service import CronService
from durin.cron.types import CronJob, CronPayload, CronSchedule


def _cron(tmp_path) -> CronService:
    return CronService(Path(tmp_path) / "cron" / "jobs.json")


def _spec(enabled=True, triggers=None):
    return parse_automation({
        "name": "briefing", "workflow": "w", "enabled": enabled,
        "triggers": triggers if triggers is not None else [
            {
                "source": "schedule",
                "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                "task": "brief me",
            },
        ],
    })


def test_sync_registers_automation_trigger_job(tmp_path):
    cron = _cron(tmp_path)
    sync_automation_jobs(cron, _spec())
    job = cron.get_job(automation_job_id("briefing", 0))
    assert job is not None
    assert job.payload.kind == "automation_trigger"
    assert job.payload.automation == "briefing"
    assert job.payload.message == "brief me"
    assert job.schedule.expr == "0 7 * * *"


def test_sync_removes_dropped_and_disabled(tmp_path):
    cron = _cron(tmp_path)
    sync_automation_jobs(cron, _spec())
    sync_automation_jobs(cron, _spec(triggers=[]))            # trigger removed from spec
    assert cron.get_job(automation_job_id("briefing", 0)) is None
    sync_automation_jobs(cron, _spec())
    sync_automation_jobs(cron, _spec(enabled=False))          # automation paused
    assert cron.get_job(automation_job_id("briefing", 0)) is None


def test_disabled_automation_wants_zero_jobs(tmp_path):
    cron = _cron(tmp_path)
    sync_automation_jobs(cron, _spec(enabled=False))
    assert cron.list_jobs(include_disabled=True) == []


def test_remove_and_boot_sync(tmp_path):
    cron = _cron(tmp_path)
    save_automation(tmp_path, _spec())
    sync_all(cron, tmp_path)
    assert cron.get_job(automation_job_id("briefing", 0)) is not None
    remove_automation_jobs(cron, "briefing")
    assert cron.get_job(automation_job_id("briefing", 0)) is None


def test_sync_skips_non_schedule_triggers_and_keeps_index(tmp_path):
    cron = _cron(tmp_path)
    spec = _spec(triggers=[
        {
            "source": "schedule",
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
            "task": "brief me",
        },
        {"source": "channel", "channel": "email"},
    ])
    sync_automation_jobs(cron, spec)
    jobs = cron.list_jobs(include_disabled=True)
    assert len(jobs) == 1
    job = cron.get_job(automation_job_id("briefing", 0))
    assert job is not None
    assert job.payload.kind == "automation_trigger" and job.payload.automation == "briefing"
    assert cron.get_job(automation_job_id("briefing", 1)) is None


def test_sync_channel_only_automation_registers_no_jobs(tmp_path):
    cron = _cron(tmp_path)
    spec = _spec(triggers=[{"source": "channel", "channel": "email"}])
    sync_automation_jobs(cron, spec)
    assert cron.list_jobs(include_disabled=True) == []


def test_sync_webhook_only_automation_registers_no_jobs(tmp_path):
    cron = _cron(tmp_path)
    spec = _spec(triggers=[{"source": "webhook", "hook": "deploy-done"}])
    sync_automation_jobs(cron, spec)
    assert cron.list_jobs(include_disabled=True) == []


def test_sync_disable_removes_mixed_trigger_jobs(tmp_path):
    cron = _cron(tmp_path)
    mixed_triggers = [
        {
            "source": "schedule",
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
            "task": "brief me",
        },
        {"source": "channel", "channel": "email"},
    ]
    sync_automation_jobs(cron, _spec(triggers=mixed_triggers))
    assert cron.get_job(automation_job_id("briefing", 0)) is not None
    sync_automation_jobs(cron, _spec(triggers=mixed_triggers, enabled=False))
    assert cron.get_job(automation_job_id("briefing", 0)) is None


# --- prune/protection cases beyond the loops reference ---------------------


def test_automation_trigger_jobs_are_protected_from_public_remove(tmp_path):
    """automation_trigger is a protected kind (unlike loop_trigger): the
    public remove_job API must refuse, even though sync's own removal path
    (exercised next) still works."""
    cron = _cron(tmp_path)
    sync_automation_jobs(cron, _spec())
    job_id = automation_job_id("briefing", 0)
    assert cron.remove_job(job_id) == "protected"
    assert cron.get_job(job_id) is not None

    sync_automation_jobs(cron, _spec(triggers=[]))  # trigger dropped from spec
    assert cron.get_job(job_id) is None  # sync's bypass still pruned it


def test_sync_all_prunes_orphaned_automation_jobs(tmp_path):
    """An automation:* job whose owning definition file disappeared from
    disk (without going through remove_automation_jobs/delete_automation)
    must still be pruned by sync_all."""
    cron = _cron(tmp_path)
    save_automation(tmp_path, _spec())
    sync_all(cron, tmp_path)
    assert cron.get_job(automation_job_id("briefing", 0)) is not None

    (Path(tmp_path) / "automations" / "briefing.json").unlink()
    sync_all(cron, tmp_path)
    assert cron.get_job(automation_job_id("briefing", 0)) is None


def test_sync_all_prunes_every_legacy_loop_job_unconditionally(tmp_path):
    """sync_all is the self-healing backstop for the loops cutover (spec
    §9): it prunes every loop:* job unconditionally, regardless of whether
    a same-named automation exists."""
    cron = _cron(tmp_path)
    cron.register_system_job(CronJob(
        id="loop:briefing:0",
        name="loop briefing trigger 0",
        schedule=CronSchedule(kind="cron", expr="0 7 * * *", tz="UTC"),
        payload=CronPayload(kind="loop_trigger", loop="briefing"),
    ))
    save_automation(tmp_path, _spec())  # same name — must not save the loop job

    sync_all(cron, tmp_path)

    assert cron.get_job("loop:briefing:0") is None
    assert cron.get_job(automation_job_id("briefing", 0)) is not None


def test_sync_all_leaves_other_system_jobs_untouched(tmp_path):
    """sync_all only ever looks at the automation: and loop: id prefixes —
    other system jobs (e.g. memory_dream) are never touched."""
    cron = _cron(tmp_path)
    cron.register_system_job(CronJob(
        id="memory_dream",
        name="memory_dream",
        schedule=CronSchedule(kind="cron", expr="0 3 * * *", tz="UTC"),
        payload=CronPayload(kind="system_event"),
    ))
    sync_all(cron, tmp_path)
    assert cron.get_job("memory_dream") is not None
