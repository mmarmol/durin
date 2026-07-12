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
        lambda d: d.__setitem__("stuck_after", True),
        lambda d: d["goal"].__setitem__("checks", [{"kind": "script", "required": True}]),  # no command
        lambda d: d["goal"].__setitem__("checks", [{"kind": "assertion", "required": True}]),  # no text
        lambda d: d.__setitem__("triggers", [{"source": "mail"}]),  # unsupported source
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


def test_parse_channel_trigger_roundtrip():
    data = _minimal()
    data["triggers"] = [
        {
            "source": "channel",
            "channel": "email",
            "filters": {"from_contains": "boss@example.com", "subject_contains": "urgent"},
            "semantic": "customer sounds upset",
            "match": "always_new",
        }
    ]
    spec = parse_loop(data)
    trig = spec.triggers[0]
    assert trig.source == "channel"
    assert trig.channel == "email"
    assert trig.filters == {"from_contains": "boss@example.com", "subject_contains": "urgent"}
    assert trig.semantic == "customer sounds upset"
    assert trig.match == "always_new"
    assert trig.schedule == {}
    assert parse_loop(loop_to_dict(spec)) == spec


def test_parse_channel_trigger_defaults():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email"}]
    spec = parse_loop(data)
    trig = spec.triggers[0]
    assert trig.filters == {}
    assert trig.semantic is None
    assert trig.match == "wake_or_new"
    assert parse_loop(loop_to_dict(spec)) == spec


def test_parse_rejects_unknown_filter_key():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "filters": {"body_contains": "x"}}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_bad_channel():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "sms"}]
    with pytest.raises(LoopError):
        parse_loop(data)


@pytest.mark.parametrize("channel", ["email", "telegram", "slack", "discord", "whatsapp"])
def test_parse_channel_trigger_all_channels_roundtrip(channel):
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": channel}]
    spec = parse_loop(data)
    assert spec.triggers[0].channel == channel
    assert parse_loop(loop_to_dict(spec)) == spec


def test_parse_channel_trigger_new_filter_keys():
    data = _minimal()
    data["triggers"] = [
        {
            "source": "channel",
            "channel": "slack",
            "filters": {"sender_contains": "alice", "text_contains": "urgent"},
        }
    ]
    spec = parse_loop(data)
    trig = spec.triggers[0]
    assert trig.filters == {"sender_contains": "alice", "text_contains": "urgent"}
    assert parse_loop(loop_to_dict(spec)) == spec


def test_parse_channel_trigger_correlate_roundtrip():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "slack", "correlate": r"ticket-(\d+)"}]
    spec = parse_loop(data)
    assert spec.triggers[0].correlate == r"ticket-(\d+)"
    assert parse_loop(loop_to_dict(spec)) == spec


def test_parse_channel_trigger_correlate_default_none():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email"}]
    spec = parse_loop(data)
    assert spec.triggers[0].correlate is None
    assert "correlate" not in loop_to_dict(spec)["triggers"][0]


def test_parse_rejects_correlate_zero_groups():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "correlate": "no-group-here"}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_correlate_two_groups():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "correlate": r"(\w+)-(\d+)"}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_correlate_uncompilable():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "correlate": "(unclosed"}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_channel_with_hook():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "hook": "some-hook"}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_webhook_trigger_roundtrip():
    data = _minimal()
    data["triggers"] = [
        {"source": "webhook", "hook": "deploy-done", "semantic": "build succeeded", "correlate": r"run-(\d+)"}
    ]
    spec = parse_loop(data)
    trig = spec.triggers[0]
    assert trig.source == "webhook"
    assert trig.hook == "deploy-done"
    assert trig.semantic == "build succeeded"
    assert trig.correlate == r"run-(\d+)"
    assert trig.schedule == {}
    assert trig.channel is None
    assert parse_loop(loop_to_dict(spec)) == spec


def test_parse_webhook_trigger_minimal_roundtrip():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "deploy-done"}]
    spec = parse_loop(data)
    trig = spec.triggers[0]
    assert trig.semantic is None
    assert trig.correlate is None
    assert parse_loop(loop_to_dict(spec)) == spec
    assert set(loop_to_dict(spec)["triggers"][0]) == {"source", "hook"}


def test_parse_rejects_webhook_bad_hook_name():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "Bad Name!"}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_webhook_with_filters():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "deploy-done", "filters": {"from_contains": "x"}}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_webhook_with_schedule():
    data = _minimal()
    data["triggers"] = [
        {"source": "webhook", "hook": "deploy-done", "schedule": {"kind": "cron", "expr": "0 8 * * *"}}
    ]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_webhook_with_channel():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "deploy-done", "channel": "email"}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_webhook_with_match():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "deploy-done", "match": "always_new"}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_cron_with_hook():
    data = _minimal()
    data["triggers"] = [
        {"source": "cron", "schedule": {"kind": "cron", "expr": "0 8 * * *"}, "hook": "deploy-done"}
    ]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_cron_with_correlate():
    data = _minimal()
    data["triggers"] = [
        {"source": "cron", "schedule": {"kind": "cron", "expr": "0 8 * * *"}, "correlate": r"(\d+)"}
    ]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_unknown_source():
    data = _minimal()
    data["triggers"] = [{"source": "webhookz", "hook": "deploy-done"}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_bad_match():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "match": "sometimes"}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_cron_with_filters():
    data = _minimal()
    data["triggers"] = [
        {"source": "cron", "schedule": {"kind": "cron", "expr": "0 8 * * *"}, "filters": {"from_contains": "x"}}
    ]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_channel_with_schedule():
    data = _minimal()
    data["triggers"] = [
        {"source": "channel", "channel": "email", "schedule": {"kind": "cron", "expr": "0 8 * * *"}}
    ]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_parse_rejects_empty_string_filter_value():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "filters": {"from_contains": "  "}}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_checks_sufficient_defaults_false():
    assert parse_loop(_minimal()).checks_sufficient is False


def test_checks_sufficient_valid_script_only():
    data = _minimal()
    data["goal"]["checks_sufficient"] = True
    data["goal"]["checks"] = [{"kind": "script", "required": True, "command": "true"}]
    spec = parse_loop(data)
    assert spec.checks_sufficient is True


def test_checks_sufficient_rejects_assertion_check():
    data = _minimal()
    data["goal"]["checks_sufficient"] = True
    data["goal"]["checks"] = [
        {"kind": "script", "required": True, "command": "true"},
        {"kind": "assertion", "required": False, "text": "looks good"},
    ]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_checks_sufficient_rejects_no_required_check():
    data = _minimal()
    data["goal"]["checks_sufficient"] = True
    data["goal"]["checks"] = [{"kind": "script", "required": False, "command": "true"}]
    with pytest.raises(LoopError):
        parse_loop(data)


def test_checks_sufficient_rejects_empty_checks():
    data = _minimal()
    data["goal"]["checks_sufficient"] = True
    data["goal"]["checks"] = []
    with pytest.raises(LoopError):
        parse_loop(data)


def test_checks_sufficient_roundtrip():
    data = _minimal()
    data["goal"]["checks_sufficient"] = True
    data["goal"]["checks"] = [{"kind": "script", "required": True, "command": "true"}]
    spec = parse_loop(data)
    assert parse_loop(loop_to_dict(spec)) == spec
    assert loop_to_dict(spec)["goal"]["checks_sufficient"] is True
