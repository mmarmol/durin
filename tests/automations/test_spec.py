import logging

import pytest

from durin.automations.spec import (
    AutomationError,
    AutomationSpec,
    AutomationTrigger,
    Delivery,
    Help,
    Life,
    automation_to_dict,
    parse_automation,
)


def _minimal() -> dict:
    return {"name": "certs", "workflow": "renew-certs"}


# --- happy path + defaults -------------------------------------------------


def test_parse_minimal_defaults():
    spec = parse_automation(_minimal())
    assert spec.name == "certs"
    assert spec.workflow == "renew-certs"
    assert spec.enabled is True
    assert spec.triggers == ()
    assert spec.delivery == Delivery()
    assert spec.help == Help()
    assert spec.life is None
    assert spec.concurrency == "single"


def test_minimal_roundtrip():
    spec = parse_automation(_minimal())
    assert parse_automation(automation_to_dict(spec)) == spec


def test_parse_full_roundtrip():
    data = _minimal() | {
        "enabled": False,
        "concurrency": "parallel",
        "triggers": [
            {"source": "schedule", "schedule": {"kind": "cron", "expr": "0 8 * * 1-5", "tz": "UTC"}, "task": "renew certs"}
        ],
        "delivery": {
            "channel": "slack",
            "to": "C0ABC123",
            "notify": "when_notable",
            "silent_labels": ["NOTHING_TO_REPORT", "ALREADY_DONE"],
        },
        "help": {"channel": "telegram", "to": "12345"},
        "life": {
            "intent": "certs renewed",
            "achieved_when": "label:COBRADA",
            "max_attempts": 5,
            "on_stuck": "escalate_pause",
        },
    }
    spec = parse_automation(data)
    assert spec.enabled is False
    assert spec.concurrency == "parallel"
    assert spec.triggers[0] == AutomationTrigger(
        source="schedule",
        schedule={"kind": "cron", "expr": "0 8 * * 1-5", "tz": "UTC"},
        task="renew certs",
    )
    assert spec.delivery == Delivery(
        channel="slack", to="C0ABC123", notify="when_notable", silent_labels=("NOTHING_TO_REPORT", "ALREADY_DONE")
    )
    assert spec.help == Help(channel="telegram", to="12345")
    assert spec.life == Life(
        intent="certs renewed", achieved_when="label:COBRADA", max_attempts=5, on_stuck="escalate_pause"
    )
    assert parse_automation(automation_to_dict(spec)) == spec


# --- name / workflow ---------------------------------------------------------


def test_parse_rejects_non_dict_data():
    with pytest.raises(AutomationError):
        parse_automation(["not", "a", "dict"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("name"),
        lambda d: d.pop("workflow"),
        lambda d: d.__setitem__("name", "Bad Name!"),
        lambda d: d.__setitem__("name", ""),
        lambda d: d.__setitem__("workflow", ""),
        lambda d: d.__setitem__("workflow", "   "),
        lambda d: d.__setitem__("concurrency", "queue"),
    ],
)
def test_parse_rejects_malformed_top_level(mutate):
    data = _minimal()
    mutate(data)
    with pytest.raises(AutomationError):
        parse_automation(data)


@pytest.mark.parametrize("concurrency", ["single", "parallel"])
def test_parse_accepts_valid_concurrency(concurrency):
    data = _minimal() | {"concurrency": concurrency}
    assert parse_automation(data).concurrency == concurrency


# --- trigger source dispatch --------------------------------------------------


def test_parse_rejects_unknown_trigger_source():
    data = _minimal()
    data["triggers"] = [{"source": "mail"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


# --- schedule trigger ---------------------------------------------------------


def _with_schedule(schedule: dict, task: str = "do the thing") -> dict:
    data = _minimal()
    data["triggers"] = [{"source": "schedule", "schedule": schedule, "task": task}]
    return data


def test_parse_schedule_trigger_roundtrip_with_tz():
    data = _with_schedule({"kind": "cron", "expr": "0 8 * * 1-5", "tz": "UTC"})
    spec = parse_automation(data)
    trig = spec.triggers[0]
    assert trig.source == "schedule"
    assert trig.schedule == {"kind": "cron", "expr": "0 8 * * 1-5", "tz": "UTC"}
    assert trig.task == "do the thing"
    assert parse_automation(automation_to_dict(spec)) == spec


def test_parse_rejects_schedule_without_task():
    data = _minimal()
    data["triggers"] = [{"source": "schedule", "schedule": {"kind": "cron", "expr": "0 8 * * *"}}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_schedule_with_blank_task():
    data = _with_schedule({"kind": "cron", "expr": "0 8 * * *"}, task="   ")
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_misnamed_schedule_key():
    # "timezone" instead of "tz" would otherwise raise TypeError deep inside
    # whatever later builds CronSchedule(**trig.schedule) from this, well
    # after the automation was already saved.
    with pytest.raises(AutomationError):
        parse_automation(_with_schedule({"kind": "cron", "expr": "0 8 * * *", "timezone": "UTC"}))


def test_parse_rejects_bad_schedule_kind():
    with pytest.raises(AutomationError):
        parse_automation(_with_schedule({"kind": "nope"}))


def test_parse_rejects_bad_cron_expr():
    with pytest.raises(AutomationError):
        parse_automation(_with_schedule({"kind": "cron", "expr": "not a cron expr"}))


def test_parse_rejects_cron_without_expr():
    with pytest.raises(AutomationError):
        parse_automation(_with_schedule({"kind": "cron"}))


def test_parse_rejects_bad_timezone():
    with pytest.raises(AutomationError):
        parse_automation(_with_schedule({"kind": "cron", "expr": "0 8 * * *", "tz": "Mars/Olympus"}))


def test_parse_rejects_every_without_every_ms():
    with pytest.raises(AutomationError):
        parse_automation(_with_schedule({"kind": "every"}))


def test_parse_rejects_every_ms_bool():
    with pytest.raises(AutomationError):
        parse_automation(_with_schedule({"kind": "every", "every_ms": True}))


def test_parse_rejects_at_without_at_ms():
    with pytest.raises(AutomationError):
        parse_automation(_with_schedule({"kind": "at"}))


def test_parse_rejects_schedule_with_channel_field():
    data = _minimal()
    data["triggers"] = [
        {"source": "schedule", "schedule": {"kind": "cron", "expr": "0 8 * * *"}, "task": "x", "channel": "email"}
    ]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_schedule_with_hook():
    data = _minimal()
    data["triggers"] = [
        {"source": "schedule", "schedule": {"kind": "cron", "expr": "0 8 * * *"}, "task": "x", "hook": "deploy-done"}
    ]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_schedule_with_correlate():
    data = _minimal()
    data["triggers"] = [
        {"source": "schedule", "schedule": {"kind": "cron", "expr": "0 8 * * *"}, "task": "x", "correlate": r"(\d+)"}
    ]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_schedule_with_chain_automation():
    data = _minimal()
    data["triggers"] = [
        {
            "source": "schedule",
            "schedule": {"kind": "cron", "expr": "0 8 * * *"},
            "task": "x",
            "chain_automation": "other",
        }
    ]
    with pytest.raises(AutomationError):
        parse_automation(data)


# --- channel trigger -----------------------------------------------------


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
    spec = parse_automation(data)
    trig = spec.triggers[0]
    assert trig.source == "channel"
    assert trig.channel == "email"
    assert trig.filters == {"from_contains": "boss@example.com", "subject_contains": "urgent"}
    assert trig.semantic == "customer sounds upset"
    assert trig.match == "always_new"
    assert trig.schedule == {}
    assert trig.task == ""
    assert parse_automation(automation_to_dict(spec)) == spec


def test_parse_channel_trigger_defaults():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email"}]
    spec = parse_automation(data)
    trig = spec.triggers[0]
    assert trig.filters == {}
    assert trig.semantic is None
    assert trig.match == "wake_or_new"
    assert parse_automation(automation_to_dict(spec)) == spec


@pytest.mark.parametrize("channel", ["email", "telegram", "slack", "discord", "whatsapp"])
def test_parse_channel_trigger_all_channels_roundtrip(channel):
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": channel}]
    spec = parse_automation(data)
    assert spec.triggers[0].channel == channel
    assert parse_automation(automation_to_dict(spec)) == spec


def test_parse_rejects_bad_channel():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "sms"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_bad_match():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "match": "sometimes"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_empty_string_filter_value():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "filters": {"from_contains": "  "}}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_non_dict_filters():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "filters": "from boss"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_blank_semantic_on_channel():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "semantic": "   "}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_channel_with_schedule():
    data = _minimal()
    data["triggers"] = [
        {"source": "channel", "channel": "email", "schedule": {"kind": "cron", "expr": "0 8 * * *"}}
    ]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_channel_with_task():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "task": "do a thing"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_channel_with_hook():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "hook": "some-hook"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_channel_with_chain_automation():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "chain_automation": "other"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_warns_but_keeps_undeclared_filter_key(caplog):
    """The filter vocabulary is open: a key the channel never populates is
    kept and warned about, not rejected. Rejecting would make the per-channel
    declaration able to block a trigger that works."""
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "filters": {"body_contains": "x"}}]

    with caplog.at_level(logging.WARNING, logger="durin.automations.spec"):
        spec = parse_automation(data)

    assert spec.triggers[0].filters == {"body_contains": "x"}
    assert any("body_contains" in r.getMessage() for r in caplog.records)


def test_parse_does_not_warn_on_a_key_the_channel_declares(caplog):
    data = _minimal()
    data["triggers"] = [
        {"source": "channel", "channel": "slack", "filters": {"chat": "C0ABC123", "sender_kind": "bot"}}
    ]
    with caplog.at_level(logging.WARNING, logger="durin.automations.spec"):
        spec = parse_automation(data)
    assert spec.triggers[0].filters == {"chat": "C0ABC123", "sender_kind": "bot"}
    assert caplog.records == []


def test_parse_channel_trigger_correlate_roundtrip():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "slack", "correlate": r"ticket-(\d+)"}]
    spec = parse_automation(data)
    assert spec.triggers[0].correlate == r"ticket-(\d+)"
    assert parse_automation(automation_to_dict(spec)) == spec


def test_parse_channel_trigger_correlate_default_none():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email"}]
    spec = parse_automation(data)
    assert spec.triggers[0].correlate is None
    assert "correlate" not in automation_to_dict(spec)["triggers"][0]


def test_parse_rejects_correlate_zero_groups():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "correlate": "no-group-here"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_correlate_two_groups():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "correlate": r"(\w+)-(\d+)"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_correlate_uncompilable():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "correlate": "(unclosed"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_blank_correlate():
    data = _minimal()
    data["triggers"] = [{"source": "channel", "channel": "email", "correlate": "   "}]
    with pytest.raises(AutomationError):
        parse_automation(data)


# --- webhook trigger -----------------------------------------------------


def test_parse_webhook_trigger_roundtrip():
    data = _minimal()
    data["triggers"] = [
        {"source": "webhook", "hook": "deploy-done", "semantic": "build succeeded", "correlate": r"run-(\d+)"}
    ]
    spec = parse_automation(data)
    trig = spec.triggers[0]
    assert trig.source == "webhook"
    assert trig.hook == "deploy-done"
    assert trig.semantic == "build succeeded"
    assert trig.correlate == r"run-(\d+)"
    assert trig.schedule == {}
    assert trig.channel is None
    assert parse_automation(automation_to_dict(spec)) == spec


def test_parse_webhook_trigger_minimal_roundtrip():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "deploy-done"}]
    spec = parse_automation(data)
    trig = spec.triggers[0]
    assert trig.semantic is None
    assert trig.correlate is None
    assert parse_automation(automation_to_dict(spec)) == spec
    assert set(automation_to_dict(spec)["triggers"][0]) == {"source", "hook"}


def test_parse_rejects_webhook_bad_hook_name():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "Bad Name!"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_blank_semantic_on_webhook():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "deploy-done", "semantic": "   "}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_webhook_with_filters():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "deploy-done", "filters": {"from_contains": "x"}}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_webhook_with_schedule():
    data = _minimal()
    data["triggers"] = [
        {"source": "webhook", "hook": "deploy-done", "schedule": {"kind": "cron", "expr": "0 8 * * *"}}
    ]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_webhook_with_task():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "deploy-done", "task": "x"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_webhook_with_channel():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "deploy-done", "channel": "email"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_webhook_with_match():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "deploy-done", "match": "always_new"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_webhook_with_chain_when():
    data = _minimal()
    data["triggers"] = [{"source": "webhook", "hook": "deploy-done", "chain_when": "achieved"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


# --- chain trigger -----------------------------------------------------


def test_parse_chain_trigger_roundtrip():
    data = _minimal()
    data["triggers"] = [{"source": "chain", "chain_automation": "upstream-automation", "chain_when": "achieved"}]
    spec = parse_automation(data)
    trig = spec.triggers[0]
    assert trig.source == "chain"
    assert trig.chain_automation == "upstream-automation"
    assert trig.chain_when == "achieved"
    assert trig.correlate is None
    assert parse_automation(automation_to_dict(spec)) == spec


def test_parse_chain_trigger_default_chain_when():
    data = _minimal()
    data["triggers"] = [{"source": "chain", "chain_automation": "upstream-automation"}]
    spec = parse_automation(data)
    assert spec.triggers[0].chain_when == "any"
    assert parse_automation(automation_to_dict(spec)) == spec


@pytest.mark.parametrize("chain_when", ["achieved", "completed", "failed", "any"])
def test_parse_chain_trigger_all_chain_when_roundtrip(chain_when):
    data = _minimal()
    data["triggers"] = [{"source": "chain", "chain_automation": "upstream-automation", "chain_when": chain_when}]
    spec = parse_automation(data)
    assert spec.triggers[0].chain_when == chain_when
    assert parse_automation(automation_to_dict(spec)) == spec


def test_parse_rejects_chain_without_chain_automation():
    data = _minimal()
    data["triggers"] = [{"source": "chain"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_chain_with_bad_chain_automation_name():
    data = _minimal()
    data["triggers"] = [{"source": "chain", "chain_automation": "Bad Name!"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_chain_with_bad_chain_when():
    data = _minimal()
    data["triggers"] = [{"source": "chain", "chain_automation": "upstream", "chain_when": "sometimes"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_chain_with_channel():
    data = _minimal()
    data["triggers"] = [{"source": "chain", "chain_automation": "upstream", "channel": "email"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_chain_with_hook():
    data = _minimal()
    data["triggers"] = [{"source": "chain", "chain_automation": "upstream", "hook": "deploy-done"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_chain_with_schedule():
    data = _minimal()
    data["triggers"] = [
        {"source": "chain", "chain_automation": "upstream", "schedule": {"kind": "cron", "expr": "0 8 * * *"}}
    ]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_chain_with_task():
    data = _minimal()
    data["triggers"] = [{"source": "chain", "chain_automation": "upstream", "task": "x"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_chain_with_semantic():
    data = _minimal()
    data["triggers"] = [{"source": "chain", "chain_automation": "upstream", "semantic": "looks urgent"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_chain_with_correlate():
    # correlate is a channel/webhook-only field: it is only ever consulted
    # while matching an inbound channel message or webhook payload, so on a
    # chain trigger it would be an accepted-but-permanently-dead field.
    # Rejected outright, exactly like schedule rejects it.
    data = _minimal()
    data["triggers"] = [{"source": "chain", "chain_automation": "upstream", "correlate": r"ticket-(\d+)"}]
    with pytest.raises(AutomationError):
        parse_automation(data)


# --- delivery ----------------------------------------------------------------


@pytest.mark.parametrize("notify", ["always", "failures_only", "when_notable", "never"])
def test_parse_accepts_valid_notify(notify):
    data = _minimal() | {"delivery": {"notify": notify}}
    assert parse_automation(data).delivery.notify == notify


def test_parse_rejects_bad_notify():
    data = _minimal() | {"delivery": {"notify": "sometimes"}}
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_delivery_silent_labels_default():
    spec = parse_automation(_minimal())
    assert spec.delivery.silent_labels == ("NOTHING_TO_REPORT",)


def test_delivery_explicit_null_silent_labels_falls_back_to_default():
    """An explicit JSON null (as opposed to omitting the key) must not crash
    the parser with a bare TypeError; it is treated the same as omitted,
    like every other list/dict-shaped field here."""
    data = _minimal() | {"delivery": {"silent_labels": None}}
    spec = parse_automation(data)
    assert spec.delivery.silent_labels == ("NOTHING_TO_REPORT",)


def test_delivery_explicit_empty_silent_labels_means_nothing_is_silent():
    """Absent or null means "use the default silence list"; an explicit []
    is a different, legal configuration meaning no label is ever silenced
    (every completed run is notable under notify: when_notable). These must
    not collapse to the same value."""
    data = _minimal() | {"delivery": {"silent_labels": []}}
    spec = parse_automation(data)
    assert spec.delivery.silent_labels == ()
    assert parse_automation(automation_to_dict(spec)) == spec


def test_parse_rejects_empty_string_silent_label():
    data = _minimal() | {"delivery": {"silent_labels": ["ACHIEVED", ""]}}
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_whitespace_only_silent_label():
    data = _minimal() | {"delivery": {"silent_labels": ["   "]}}
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_non_dict_delivery():
    data = _minimal() | {"delivery": "slack"}
    with pytest.raises(AutomationError):
        parse_automation(data)


# --- help ----------------------------------------------------------------


def test_parse_help_roundtrip():
    data = _minimal() | {"help": {"channel": "telegram", "to": "12345"}}
    spec = parse_automation(data)
    assert spec.help == Help(channel="telegram", to="12345")
    assert parse_automation(automation_to_dict(spec)) == spec


def test_parse_rejects_non_dict_help():
    data = _minimal() | {"help": "telegram"}
    with pytest.raises(AutomationError):
        parse_automation(data)


# --- life ----------------------------------------------------------------


def test_parse_rejects_non_dict_life():
    data = _minimal() | {"life": "certs renewed"}
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_life_without_intent():
    data = _minimal() | {"life": {}}
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_rejects_life_with_blank_intent():
    data = _minimal() | {"life": {"intent": "   "}}
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_life_defaults():
    data = _minimal() | {"life": {"intent": "certs renewed"}}
    life = parse_automation(data).life
    assert life.achieved_when == "any_completed"
    assert life.max_attempts is None
    assert life.on_stuck == "notify"


@pytest.mark.parametrize("achieved_when", ["any_completed", "label:COBRADA", "label:x"])
def test_parse_accepts_valid_achieved_when(achieved_when):
    data = _minimal() | {"life": {"intent": "x", "achieved_when": achieved_when}}
    assert parse_automation(data).life.achieved_when == achieved_when


@pytest.mark.parametrize("achieved_when", ["label:", "sometimes", "COBRADA", "label", ""])
def test_parse_rejects_bad_achieved_when(achieved_when):
    data = _minimal() | {"life": {"intent": "x", "achieved_when": achieved_when}}
    with pytest.raises(AutomationError):
        parse_automation(data)


@pytest.mark.parametrize("on_stuck", ["escalate_pause", "notify", "keep"])
def test_parse_accepts_valid_on_stuck(on_stuck):
    data = _minimal() | {"life": {"intent": "x", "on_stuck": on_stuck}}
    assert parse_automation(data).life.on_stuck == on_stuck


def test_parse_rejects_bad_on_stuck():
    data = _minimal() | {"life": {"intent": "x", "on_stuck": "ignore"}}
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_accepts_max_attempts_none_by_default():
    data = _minimal() | {"life": {"intent": "x"}}
    assert parse_automation(data).life.max_attempts is None


@pytest.mark.parametrize("max_attempts", [0, -1, True, False, 1.5, "3"])
def test_parse_rejects_bad_max_attempts(max_attempts):
    data = _minimal() | {"life": {"intent": "x", "max_attempts": max_attempts}}
    with pytest.raises(AutomationError):
        parse_automation(data)


def test_parse_accepts_valid_max_attempts():
    data = _minimal() | {"life": {"intent": "x", "max_attempts": 5}}
    assert parse_automation(data).life.max_attempts == 5


def test_life_roundtrip():
    data = _minimal() | {
        "life": {"intent": "certs renewed", "achieved_when": "label:DONE", "max_attempts": 3, "on_stuck": "keep"}
    }
    spec = parse_automation(data)
    assert parse_automation(automation_to_dict(spec)) == spec
    assert automation_to_dict(spec)["life"] == {
        "intent": "certs renewed",
        "achieved_when": "label:DONE",
        "max_attempts": 3,
        "on_stuck": "keep",
    }


def test_life_none_roundtrips_to_none():
    spec = parse_automation(_minimal())
    assert automation_to_dict(spec)["life"] is None
