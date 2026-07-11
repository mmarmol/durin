from dataclasses import asdict

from durin.cron.service import CronService
from durin.cron.types import CronJob, CronPayload, CronRunRecord, CronSchedule, CronJobState


def test_loop_trigger_payload_survives_store_save_and_load(tmp_path):
    """The on-disk store maps payload fields explicitly (camelCase); prove the
    new ``loop`` field is included in that mapping, not just in ``asdict``."""
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)
    service.register_system_job(CronJob(
        id="loop:briefing:0",
        name="loop briefing trigger 0",
        schedule=CronSchedule(kind="cron", expr="0 7 * * *", tz="UTC"),
        payload=CronPayload(kind="loop_trigger", loop="briefing"),
    ))

    reloaded = CronService(store_path).get_job("loop:briefing:0")

    assert reloaded is not None
    assert reloaded.payload.kind == "loop_trigger"
    assert reloaded.payload.loop == "briefing"


def test_payload_carries_mode_and_model():
    p = CronPayload(kind="agent_turn", mode="task", message="do X", model="glm-5.2")
    d = asdict(p)
    assert d["mode"] == "task"
    assert d["model"] == "glm-5.2"
    assert CronPayload(**d).mode == "task"


def test_run_record_carries_session_key():
    r = CronRunRecord(run_at_ms=1, status="ok", duration_ms=2,
                      session_key="cron:abc:run:1", model="glm-5.2", summary="ok")
    assert "session_key" in asdict(r)  # asdict uses field names (snake_case)
    assert CronRunRecord(**asdict(r)).session_key == "cron:abc:run:1"
