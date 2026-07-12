from pathlib import Path

from durin.cron.service import CronService
from durin.loops.cron_sync import loop_job_id, remove_loop_jobs, sync_all, sync_loop_jobs
from durin.loops.spec import parse_loop
from durin.loops.store import save_loop


def _cron(tmp_path) -> CronService:
    return CronService(Path(tmp_path) / "cron" / "jobs.json")


def _spec(enabled=True, triggers=None):
    return parse_loop({
        "name": "briefing", "workflow": "w", "goal": {"intent": "briefed"}, "enabled": enabled,
        "triggers": triggers if triggers is not None else [
            {"source": "cron", "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}},
        ],
    })


def test_sync_registers_loop_trigger_job(tmp_path):
    cron = _cron(tmp_path)
    sync_loop_jobs(cron, _spec())
    job = cron.get_job(loop_job_id("briefing", 0))
    assert job is not None
    assert job.payload.kind == "loop_trigger" and job.payload.loop == "briefing"
    assert job.schedule.expr == "0 7 * * *"


def test_sync_removes_dropped_and_disabled(tmp_path):
    cron = _cron(tmp_path)
    sync_loop_jobs(cron, _spec())
    sync_loop_jobs(cron, _spec(triggers=[]))            # trigger removed from spec
    assert cron.get_job(loop_job_id("briefing", 0)) is None
    sync_loop_jobs(cron, _spec())
    sync_loop_jobs(cron, _spec(enabled=False))          # loop paused
    assert cron.get_job(loop_job_id("briefing", 0)) is None


def test_remove_and_boot_sync(tmp_path):
    cron = _cron(tmp_path)
    save_loop(tmp_path, _spec())
    sync_all(cron, tmp_path)
    assert cron.get_job(loop_job_id("briefing", 0)) is not None
    remove_loop_jobs(cron, "briefing")
    assert cron.get_job(loop_job_id("briefing", 0)) is None


def test_sync_skips_channel_triggers_and_keeps_cron_index(tmp_path):
    cron = _cron(tmp_path)
    spec = _spec(triggers=[
        {"source": "cron", "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}},
        {"source": "channel", "channel": "email"},
    ])
    sync_loop_jobs(cron, spec)
    jobs = cron.list_jobs(include_disabled=True)
    assert len(jobs) == 1
    job = cron.get_job(loop_job_id("briefing", 0))
    assert job is not None
    assert job.payload.kind == "loop_trigger" and job.payload.loop == "briefing"
    assert cron.get_job(loop_job_id("briefing", 1)) is None


def test_sync_channel_only_loop_registers_no_jobs(tmp_path):
    cron = _cron(tmp_path)
    spec = _spec(triggers=[{"source": "channel", "channel": "email"}])
    sync_loop_jobs(cron, spec)
    assert cron.list_jobs(include_disabled=True) == []


def test_sync_webhook_only_loop_registers_no_jobs(tmp_path):
    cron = _cron(tmp_path)
    spec = _spec(triggers=[{"source": "webhook", "hook": "deploy-done"}])
    sync_loop_jobs(cron, spec)
    assert cron.list_jobs(include_disabled=True) == []


def test_sync_disable_removes_mixed_trigger_jobs(tmp_path):
    cron = _cron(tmp_path)
    mixed_triggers = [
        {"source": "cron", "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}},
        {"source": "channel", "channel": "email"},
    ]
    sync_loop_jobs(cron, _spec(triggers=mixed_triggers))
    assert cron.get_job(loop_job_id("briefing", 0)) is not None
    sync_loop_jobs(cron, _spec(triggers=mixed_triggers, enabled=False))
    assert cron.get_job(loop_job_id("briefing", 0)) is None
