"""Automation definition schema and validation.

An automation binds triggers (a schedule, an inbound channel message, a
webhook, or the outcome of another automation via chaining) to a workflow
body, an optional delivery/help routing, and an optional life (a verifiable
"achieved" condition plus stuck-attempt handling). It never touches
workflow-engine semantics; the workflow is referenced by name only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from durin.loops.channel_meta import CHANNEL_FILTER_KEYS

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SCHEDULE_KINDS = {"at", "every", "cron"}
# Keys each schedule kind accepts. Whatever later builds
# ``CronSchedule(**trigger.schedule)`` from this at sync time would otherwise
# raise TypeError well after the definition was already saved on an unknown
# key. Reject it here instead so a bad/misnamed key (e.g. "timezone" instead
# of "tz") fails at parse time with a clear AutomationError.
_SCHEDULE_ALLOWED_KEYS = {
    "cron": {"kind", "expr", "tz"},
    "every": {"kind", "every_ms"},
    "at": {"kind", "at_ms"},
}
_CHANNEL_KINDS = {"email", "telegram", "slack", "discord", "whatsapp"}
_CHANNEL_MATCH_MODES = {"wake_or_new", "always_new"}
_NOTIFY_MODES = ("always", "failures_only", "when_notable", "never")
_ON_STUCK_MODES = ("escalate_pause", "notify", "keep")
_CHAIN_WHEN_MODES = ("achieved", "completed", "failed", "any")

# Field ownership per trigger source, used to reject foreign fields outright
# so the four trigger shapes never mix. "semantic" and "correlate" are each
# owned by more than one source (see below), everything else by exactly one.
_SCHEDULE_ONLY_FIELDS = {"schedule", "task"}
_CHANNEL_ONLY_FIELDS = {"channel", "filters", "match"}
_WEBHOOK_ONLY_FIELDS = {"hook"}
_CHAIN_ONLY_FIELDS = {"chain_automation", "chain_when"}

_SCHEDULE_FORBIDDEN_FIELDS = _CHANNEL_ONLY_FIELDS | _WEBHOOK_ONLY_FIELDS | {"semantic", "correlate"} | _CHAIN_ONLY_FIELDS
_CHANNEL_FORBIDDEN_FIELDS = _SCHEDULE_ONLY_FIELDS | _WEBHOOK_ONLY_FIELDS | _CHAIN_ONLY_FIELDS
_WEBHOOK_FORBIDDEN_FIELDS = _SCHEDULE_ONLY_FIELDS | _CHANNEL_ONLY_FIELDS | _CHAIN_ONLY_FIELDS
# "correlate" is not forbidden here: unlike a schedule fire, a chain fire
# carries the upstream automation's own final text (see AutomationsRuntime's
# chain dispatch), so a capture-group pattern has something to match against.
# It stays inert without a channel/webhook trigger in the same automation
# (nothing else ever consults a chain trigger's correlate) — that combination
# is warned about below, not rejected.
_CHAIN_FORBIDDEN_FIELDS = _SCHEDULE_ONLY_FIELDS | _CHANNEL_ONLY_FIELDS | _WEBHOOK_ONLY_FIELDS | {"semantic"}


class AutomationError(ValueError):
    """Malformed automation definition."""


@dataclass(frozen=True)
class Delivery:
    channel: str | None = None
    to: str | None = None
    notify: Literal["always", "failures_only", "when_notable", "never"] = "always"
    silent_labels: tuple[str, ...] = ("NOTHING_TO_REPORT",)


@dataclass(frozen=True)
class Help:
    channel: str | None = None
    to: str | None = None


@dataclass(frozen=True)
class Life:
    intent: str
    achieved_when: str = "any_completed"  # or "label:<LABEL>"
    max_attempts: int | None = None  # None = never stuck
    on_stuck: Literal["escalate_pause", "notify", "keep"] = "notify"


@dataclass(frozen=True)
class AutomationTrigger:
    source: Literal["schedule", "channel", "webhook", "chain"]
    schedule: dict = field(default_factory=dict)  # CronSchedule-shaped: kind/at_ms/every_ms/expr/tz
    task: str = ""  # schedule-only: the clock fire's task text
    channel: str | None = None  # channel trigger only: email/telegram/slack/discord/whatsapp
    filters: dict = field(default_factory=dict)  # channel trigger only: open key->value map
    semantic: str | None = None  # channel or webhook trigger: optional model-judged condition
    match: Literal["wake_or_new", "always_new"] = "wake_or_new"  # channel trigger only
    hook: str | None = None  # webhook trigger only: the hook name that fires this trigger
    correlate: str | None = None  # channel, webhook, or chain trigger: optional single-capture-group regex
    chain_automation: str | None = None  # chain trigger only: the upstream automation's name
    chain_when: Literal["achieved", "completed", "failed", "any"] = "any"  # chain trigger only


@dataclass(frozen=True)
class AutomationSpec:
    name: str
    workflow: str
    enabled: bool = True
    triggers: tuple[AutomationTrigger, ...] = ()
    delivery: Delivery = Delivery()
    help: Help = Help()
    life: Life | None = None
    concurrency: Literal["single", "parallel"] = "single"


def _parse_correlate(raw: dict, i: int) -> str | None:
    correlate = raw.get("correlate")
    if correlate is None:
        return None
    if not isinstance(correlate, str) or not correlate.strip():
        raise AutomationError(f"trigger[{i}]: correlate must be a non-empty string if set")
    try:
        pattern = re.compile(correlate)
    except re.error:
        raise AutomationError(f"trigger[{i}]: correlate must be a regex with exactly one capture group") from None
    if pattern.groups != 1:
        raise AutomationError(f"trigger[{i}]: correlate must be a regex with exactly one capture group")
    return correlate


def _parse_schedule_trigger(raw: dict, i: int) -> AutomationTrigger:
    present = _SCHEDULE_FORBIDDEN_FIELDS & set(raw)
    if present:
        raise AutomationError(f"trigger[{i}]: schedule trigger cannot set {sorted(present)}")
    sched = raw.get("schedule") or {}
    kind = sched.get("kind")
    if kind not in _SCHEDULE_KINDS:
        raise AutomationError(f"trigger[{i}]: schedule.kind must be one of {sorted(_SCHEDULE_KINDS)}")
    unknown = set(sched) - _SCHEDULE_ALLOWED_KEYS[kind]
    if unknown:
        raise AutomationError(
            f"trigger[{i}]: unknown schedule key(s) {sorted(unknown)} for kind {kind!r}"
        )
    if kind == "cron":
        expr = sched.get("expr")
        if not isinstance(expr, str) or not expr.strip():
            raise AutomationError(f"trigger[{i}]: cron schedule requires a non-empty 'expr'")
        try:
            from croniter import croniter
        except ImportError:
            croniter = None  # type: ignore[assignment]
        if croniter is not None:
            try:
                croniter(expr)
            except (ValueError, KeyError) as exc:
                raise AutomationError(f"trigger[{i}]: invalid cron expression {expr!r}: {exc}") from None
        tz = sched.get("tz")
        if tz is not None:
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(tz)
            except Exception:
                raise AutomationError(f"trigger[{i}]: unknown timezone {tz!r}") from None
    elif kind == "every":
        every_ms = sched.get("every_ms")
        if not isinstance(every_ms, int) or isinstance(every_ms, bool) or every_ms < 1:
            raise AutomationError(f"trigger[{i}]: 'every' schedule requires integer every_ms >= 1")
    elif kind == "at":
        at_ms = sched.get("at_ms")
        if not isinstance(at_ms, int) or isinstance(at_ms, bool) or at_ms < 1:
            raise AutomationError(f"trigger[{i}]: 'at' schedule requires integer at_ms >= 1")
    task = raw.get("task")
    if not isinstance(task, str) or not task.strip():
        raise AutomationError(f"trigger[{i}]: schedule trigger requires a non-empty 'task'")
    return AutomationTrigger(source="schedule", schedule=dict(sched), task=task.strip())


def _parse_channel_trigger(raw: dict, i: int) -> AutomationTrigger:
    present = _CHANNEL_FORBIDDEN_FIELDS & set(raw)
    if present:
        raise AutomationError(f"trigger[{i}]: channel trigger cannot set {sorted(present)}")
    channel = raw.get("channel")
    if channel not in _CHANNEL_KINDS:
        raise AutomationError(f"trigger[{i}]: channel must be one of {sorted(_CHANNEL_KINDS)}")
    filters = raw.get("filters") or {}
    if not isinstance(filters, dict):
        raise AutomationError(f"trigger[{i}]: filters must be an object")
    # Warn, never reject: the filter vocabulary is open, and a channel may
    # legitimately emit a key it did not declare. Rejecting here would make
    # the declaration a second source of truth able to block a working
    # trigger. The warning still earns its keep — an undeclared key matches
    # nothing, so without it the trigger would just stay silent forever.
    undeclared = set(filters) - CHANNEL_FILTER_KEYS.get(channel, frozenset())
    if undeclared:
        logger.warning(
            "automations: trigger[%d] filters on %s which the %s channel never populates; this trigger will not match",
            i,
            sorted(undeclared),
            channel,
        )
    for key, value in filters.items():
        if not isinstance(value, str) or not value.strip():
            raise AutomationError(f"trigger[{i}]: filter {key!r} must be a non-empty string")
    semantic = raw.get("semantic")
    if semantic is not None and not (isinstance(semantic, str) and semantic.strip()):
        raise AutomationError(f"trigger[{i}]: semantic must be a non-empty string if set")
    match = raw.get("match", "wake_or_new")
    if match not in _CHANNEL_MATCH_MODES:
        raise AutomationError(f"trigger[{i}]: match must be one of {sorted(_CHANNEL_MATCH_MODES)}")
    correlate = _parse_correlate(raw, i)
    return AutomationTrigger(
        source="channel",
        channel=channel,
        filters=dict(filters),
        semantic=semantic,
        match=match,
        correlate=correlate,
    )


def _parse_webhook_trigger(raw: dict, i: int) -> AutomationTrigger:
    present = _WEBHOOK_FORBIDDEN_FIELDS & set(raw)
    if present:
        raise AutomationError(f"trigger[{i}]: webhook trigger cannot set {sorted(present)}")
    hook = raw.get("hook")
    if not isinstance(hook, str) or not _NAME_RE.match(hook):
        raise AutomationError(f"trigger[{i}]: hook must match ^[a-z0-9][a-z0-9_-]{{0,63}}$")
    semantic = raw.get("semantic")
    if semantic is not None and not (isinstance(semantic, str) and semantic.strip()):
        raise AutomationError(f"trigger[{i}]: semantic must be a non-empty string if set")
    correlate = _parse_correlate(raw, i)
    return AutomationTrigger(source="webhook", hook=hook, semantic=semantic, correlate=correlate)


def _parse_chain_trigger(raw: dict, i: int) -> AutomationTrigger:
    present = _CHAIN_FORBIDDEN_FIELDS & set(raw)
    if present:
        raise AutomationError(f"trigger[{i}]: chain trigger cannot set {sorted(present)}")
    chain_automation = raw.get("chain_automation")
    if not isinstance(chain_automation, str) or not _NAME_RE.match(chain_automation):
        raise AutomationError(f"trigger[{i}]: chain_automation must match ^[a-z0-9][a-z0-9_-]{{0,63}}$")
    chain_when = raw.get("chain_when", "any")
    if chain_when not in _CHAIN_WHEN_MODES:
        raise AutomationError(f"trigger[{i}]: chain_when must be one of {sorted(_CHAIN_WHEN_MODES)}")
    correlate = _parse_correlate(raw, i)
    return AutomationTrigger(
        source="chain", chain_automation=chain_automation, chain_when=chain_when, correlate=correlate
    )


def _parse_trigger(raw: dict, i: int) -> AutomationTrigger:
    source = raw.get("source")
    if source not in ("schedule", "channel", "webhook", "chain"):
        raise AutomationError(f"trigger[{i}]: source must be 'schedule', 'channel', 'webhook', or 'chain'")
    if source == "channel":
        return _parse_channel_trigger(raw, i)
    if source == "webhook":
        return _parse_webhook_trigger(raw, i)
    if source == "chain":
        return _parse_chain_trigger(raw, i)
    return _parse_schedule_trigger(raw, i)


def _warn_inert_correlate(name: str, triggers: tuple[AutomationTrigger, ...]) -> None:
    """correlate is only ever consulted while matching an inbound channel
    message or webhook payload. An automation with no channel/webhook
    trigger at all has nothing to consult it, so a correlate set elsewhere
    (only reachable today via a chain trigger) will never be evaluated.
    Advisory only, like the undeclared-filter-key check above: keep the
    value, just warn."""
    if any(t.source in ("channel", "webhook") for t in triggers):
        return
    for i, t in enumerate(triggers):
        if t.correlate:
            logger.warning(
                "automations: %r trigger[%d] declares correlate but no channel/webhook trigger exists; "
                "this pattern will never be evaluated",
                name,
                i,
            )


def _parse_delivery(raw: dict) -> Delivery:
    if not isinstance(raw, dict):
        raise AutomationError("delivery must be an object")
    notify = raw.get("notify", "always")
    if notify not in _NOTIFY_MODES:
        raise AutomationError(f"delivery.notify must be one of {_NOTIFY_MODES}")
    silent_labels = tuple(raw.get("silent_labels") or ("NOTHING_TO_REPORT",))
    for label in silent_labels:
        if not isinstance(label, str) or not label.strip():
            raise AutomationError("delivery.silent_labels must all be non-empty strings")
    return Delivery(
        channel=raw.get("channel") or None,
        to=raw.get("to") or None,
        notify=notify,
        silent_labels=silent_labels,
    )


def _parse_help(raw: dict) -> Help:
    if not isinstance(raw, dict):
        raise AutomationError("help must be an object")
    return Help(channel=raw.get("channel") or None, to=raw.get("to") or None)


def _parse_life(raw: dict) -> Life:
    if not isinstance(raw, dict):
        raise AutomationError("life must be an object")
    intent = raw.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        raise AutomationError("life.intent is required")
    achieved_when = raw.get("achieved_when", "any_completed")
    valid_achieved_when = achieved_when == "any_completed" or (
        isinstance(achieved_when, str)
        and achieved_when.startswith("label:")
        and achieved_when[len("label:") :].strip()
    )
    if not valid_achieved_when:
        raise AutomationError("life.achieved_when must be 'any_completed' or 'label:<non-empty>'")
    max_attempts = raw.get("max_attempts")
    if max_attempts is not None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise AutomationError("life.max_attempts must be an integer >= 1 if set")
    on_stuck = raw.get("on_stuck", "notify")
    if on_stuck not in _ON_STUCK_MODES:
        raise AutomationError(f"life.on_stuck must be one of {_ON_STUCK_MODES}")
    return Life(intent=intent.strip(), achieved_when=achieved_when, max_attempts=max_attempts, on_stuck=on_stuck)


def parse_automation(data: dict) -> AutomationSpec:
    if not isinstance(data, dict):
        raise AutomationError("automation definition must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise AutomationError("name must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    workflow = data.get("workflow")
    if not isinstance(workflow, str) or not workflow.strip():
        raise AutomationError("workflow is required")
    triggers = tuple(_parse_trigger(t, i) for i, t in enumerate(data.get("triggers") or []))
    _warn_inert_correlate(name, triggers)
    concurrency = data.get("concurrency", "single")
    if concurrency not in ("single", "parallel"):
        raise AutomationError("concurrency must be 'single' or 'parallel'")
    life_raw = data.get("life")
    return AutomationSpec(
        name=name,
        workflow=workflow.strip(),
        enabled=bool(data.get("enabled", True)),
        triggers=triggers,
        delivery=_parse_delivery(data.get("delivery") or {}),
        help=_parse_help(data.get("help") or {}),
        life=_parse_life(life_raw) if life_raw is not None else None,
        concurrency=concurrency,
    )


def _trigger_to_dict(t: AutomationTrigger) -> dict:
    if t.source == "schedule":
        return {"source": "schedule", "schedule": t.schedule, "task": t.task}
    if t.source == "webhook":
        entry: dict = {"source": "webhook", "hook": t.hook}
        if t.semantic is not None:
            entry["semantic"] = t.semantic
        if t.correlate is not None:
            entry["correlate"] = t.correlate
        return entry
    if t.source == "chain":
        entry = {"source": "chain", "chain_automation": t.chain_automation, "chain_when": t.chain_when}
        if t.correlate is not None:
            entry["correlate"] = t.correlate
        return entry
    entry = {"source": "channel", "channel": t.channel, "filters": t.filters, "match": t.match}
    if t.semantic is not None:
        entry["semantic"] = t.semantic
    if t.correlate is not None:
        entry["correlate"] = t.correlate
    return entry


def _delivery_to_dict(d: Delivery) -> dict:
    return {"channel": d.channel, "to": d.to, "notify": d.notify, "silent_labels": list(d.silent_labels)}


def _help_to_dict(h: Help) -> dict:
    return {"channel": h.channel, "to": h.to}


def _life_to_dict(life: Life) -> dict:
    return {
        "intent": life.intent,
        "achieved_when": life.achieved_when,
        "max_attempts": life.max_attempts,
        "on_stuck": life.on_stuck,
    }


def automation_to_dict(spec: AutomationSpec) -> dict:
    return {
        "name": spec.name,
        "enabled": spec.enabled,
        "workflow": spec.workflow,
        "triggers": [_trigger_to_dict(t) for t in spec.triggers],
        "delivery": _delivery_to_dict(spec.delivery),
        "help": _help_to_dict(spec.help),
        "life": _life_to_dict(spec.life) if spec.life is not None else None,
        "concurrency": spec.concurrency,
    }
