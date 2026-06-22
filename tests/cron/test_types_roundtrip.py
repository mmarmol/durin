from dataclasses import asdict
from durin.cron.types import CronJob, CronPayload, CronRunRecord, CronSchedule, CronJobState


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
