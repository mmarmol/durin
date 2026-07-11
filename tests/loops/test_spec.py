import pytest
from durin.loops.spec import GoalCheck, LoopError, LoopSpec, LoopTrigger, loop_to_dict, parse_loop


def _minimal() -> dict:
    return {
        "name": "certs",
        "goal": {"intent": "certs renewed", "checks": []},
        "workflow": "renew-certs",
    }


def test_parse_minimal_defaults():
    spec = parse_loop(_minimal())
    assert spec.name == "certs"
    assert spec.enabled is True
    assert spec.workflow == "renew-certs"
    assert spec.goal_intent == "certs renewed"
    assert spec.checks == ()
    assert spec.triggers == ()
    assert spec.concurrency == "single"
    assert spec.stuck_after == 3
    assert spec.operator_channel is None
    assert spec.operator_to is None


def test_parse_full_roundtrip():
    data = _minimal() | {
        "enabled": False,
        "concurrency": "parallel",
        "stuck_after": 5,
        "operator_channel": "telegram",
        "operator_to": "12345",
        "goal": {
            "intent": "ticket answered",
            "checks": [
                {"kind": "script", "required": True, "command": "true"},
                {"kind": "assertion", "required": False, "text": "customer satisfied"},
            ],
        },
        "triggers": [{"source": "cron", "schedule": {"kind": "cron", "expr": "0 8 * * 1-5", "tz": "UTC"}}],
    }
    spec = parse_loop(data)
    assert spec.checks[0] == GoalCheck(kind="script", required=True, command="true", text=None)
    assert spec.checks[1].kind == "assertion"
    assert spec.triggers[0] == LoopTrigger(source="cron", schedule={"kind": "cron", "expr": "0 8 * * 1-5", "tz": "UTC"})
    assert spec.operator_to == "12345"
    assert parse_loop(loop_to_dict(spec)) == spec


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("name"),
        lambda d: d.pop("workflow"),
        lambda d: d.__setitem__("name", "bad/name"),
        lambda d: d.__setitem__("concurrency", "queue"),
        lambda d: d.__setitem__("stuck_after", 0),
        lambda d: d["goal"].__setitem__("checks", [{"kind": "script", "required": True}]),  # no command
        lambda d: d["goal"].__setitem__("checks", [{"kind": "assertion", "required": True}]),  # no text
        lambda d: d.__setitem__("triggers", [{"source": "mail"}]),  # V1: cron only
        lambda d: d.__setitem__("triggers", [{"source": "cron", "schedule": {"kind": "nope"}}]),
    ],
)
def test_parse_rejects_malformed(mutate):
    data = _minimal()
    data.setdefault("goal", {}).setdefault("checks", [])
    mutate(data)
    with pytest.raises(LoopError):
        parse_loop(data)


def _with_trigger(schedule: dict) -> dict:
    data = _minimal()
    data["triggers"] = [{"source": "cron", "schedule": schedule}]
    return data


def test_parse_rejects_misnamed_schedule_key():
    # "timezone" instead of "tz" — CronSchedule(**schedule) would otherwise
    # raise TypeError at cron-sync time, well after the loop was saved.
    with pytest.raises(LoopError):
        parse_loop(_with_trigger({"kind": "cron", "expr": "0 8 * * *", "timezone": "UTC"}))


def test_parse_rejects_bad_cron_expr():
    with pytest.raises(LoopError):
        parse_loop(_with_trigger({"kind": "cron", "expr": "not a cron expr"}))


def test_parse_rejects_every_without_every_ms():
    with pytest.raises(LoopError):
        parse_loop(_with_trigger({"kind": "every"}))


def test_parse_accepts_valid_cron_with_tz():
    spec = parse_loop(_with_trigger({"kind": "cron", "expr": "0 8 * * 1-5", "tz": "UTC"}))
    assert spec.triggers[0].schedule == {"kind": "cron", "expr": "0 8 * * 1-5", "tz": "UTC"}
