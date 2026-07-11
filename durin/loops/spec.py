"""Loop definition schema and validation.

A loop binds triggers to a workflow body and a verifiable goal. It never
touches workflow-engine semantics; the workflow is referenced by name only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SCHEDULE_KINDS = {"at", "every", "cron"}
# Keys each schedule kind accepts. `durin.loops.cron_sync.sync_loop_jobs` does
# `CronSchedule(**trig.schedule)` at sync time (boot + save) — an unknown key
# raises TypeError there, well after the definition was already saved. Reject
# it here instead so a bad/misnamed key (e.g. "timezone" instead of "tz")
# fails at parse time with a clear LoopError.
_SCHEDULE_ALLOWED_KEYS = {
    "cron": {"kind", "expr", "tz"},
    "every": {"kind", "every_ms"},
    "at": {"kind", "at_ms"},
}
# Fields that only make sense on a channel trigger. Rejected outright on a
# cron trigger (and vice versa for "schedule") so the two shapes never mix.
_CHANNEL_ONLY_FIELDS = {"channel", "filters", "semantic", "match"}
_CHANNEL_FILTER_ALLOWED_KEYS = {"from_contains", "subject_contains"}
_CHANNEL_MATCH_MODES = {"wake_or_new", "always_new"}


class LoopError(ValueError):
    """Malformed loop definition."""


class LoopNotFound(LoopError):
    """No definition file for the requested loop name."""


@dataclass(frozen=True)
class GoalCheck:
    kind: Literal["script", "assertion"]
    required: bool
    command: str | None = None  # script: shell command, exit 0 = pass
    text: str | None = None     # assertion: sentence judged against evidence


@dataclass(frozen=True)
class LoopTrigger:
    source: Literal["cron", "channel"]  # webhook sources arrive in V4
    schedule: dict = field(default_factory=dict)  # CronSchedule-shaped: kind/at_ms/every_ms/expr/tz
    channel: str | None = None  # channel trigger only; V2: only "email"
    filters: dict = field(default_factory=dict)  # channel trigger only: from_contains/subject_contains
    semantic: str | None = None  # channel trigger only: optional model-judged condition
    match: Literal["wake_or_new", "always_new"] = "wake_or_new"  # channel trigger only


@dataclass(frozen=True)
class LoopSpec:
    name: str
    workflow: str
    goal_intent: str
    checks: tuple[GoalCheck, ...] = ()
    checks_sufficient: bool = False
    triggers: tuple[LoopTrigger, ...] = ()
    enabled: bool = True
    concurrency: Literal["single", "parallel"] = "single"
    stuck_after: int = 3
    operator_channel: str | None = None
    operator_to: str | None = None


def _parse_check(raw: dict, i: int) -> GoalCheck:
    kind = raw.get("kind")
    if kind not in ("script", "assertion"):
        raise LoopError(f"check[{i}]: kind must be 'script' or 'assertion'")
    required = bool(raw.get("required", True))
    command = raw.get("command")
    text = raw.get("text")
    if kind == "script" and not (isinstance(command, str) and command.strip()):
        raise LoopError(f"check[{i}]: script check needs a non-empty 'command'")
    if kind == "assertion" and not (isinstance(text, str) and text.strip()):
        raise LoopError(f"check[{i}]: assertion check needs a non-empty 'text'")
    return GoalCheck(kind=kind, required=required, command=command, text=text)


def _parse_trigger(raw: dict, i: int) -> LoopTrigger:
    source = raw.get("source")
    if source not in ("cron", "channel"):
        raise LoopError(f"trigger[{i}]: source must be 'cron' or 'channel'")
    if source == "channel":
        return _parse_channel_trigger(raw, i)

    present = _CHANNEL_ONLY_FIELDS & set(raw)
    if present:
        raise LoopError(f"trigger[{i}]: cron trigger cannot set {sorted(present)}")
    sched = raw.get("schedule") or {}
    kind = sched.get("kind")
    if kind not in _SCHEDULE_KINDS:
        raise LoopError(f"trigger[{i}]: schedule.kind must be one of {sorted(_SCHEDULE_KINDS)}")
    unknown = set(sched) - _SCHEDULE_ALLOWED_KEYS[kind]
    if unknown:
        raise LoopError(
            f"trigger[{i}]: unknown schedule key(s) {sorted(unknown)} for kind {kind!r}"
        )
    if kind == "cron":
        expr = sched.get("expr")
        if not isinstance(expr, str) or not expr.strip():
            raise LoopError(f"trigger[{i}]: cron schedule requires a non-empty 'expr'")
        # Mirror durin.cron.service._validate_schedule_for_add's add-time check:
        # reject a bad expr here instead of letting it silently never fire.
        try:
            from croniter import croniter
        except ImportError:
            croniter = None  # type: ignore[assignment]
        if croniter is not None:
            try:
                croniter(expr)
            except (ValueError, KeyError) as exc:
                raise LoopError(f"trigger[{i}]: invalid cron expression {expr!r}: {exc}") from None
        tz = sched.get("tz")
        if tz is not None:
            try:
                from zoneinfo import ZoneInfo

                ZoneInfo(tz)
            except Exception:
                raise LoopError(f"trigger[{i}]: unknown timezone {tz!r}") from None
    elif kind == "every":
        every_ms = sched.get("every_ms")
        if not isinstance(every_ms, int) or isinstance(every_ms, bool) or every_ms < 1:
            raise LoopError(f"trigger[{i}]: 'every' schedule requires integer every_ms >= 1")
    elif kind == "at":
        at_ms = sched.get("at_ms")
        if not isinstance(at_ms, int) or isinstance(at_ms, bool) or at_ms < 1:
            raise LoopError(f"trigger[{i}]: 'at' schedule requires integer at_ms >= 1")
    return LoopTrigger(source="cron", schedule=dict(sched))


def _parse_channel_trigger(raw: dict, i: int) -> LoopTrigger:
    if "schedule" in raw:
        raise LoopError(f"trigger[{i}]: channel trigger cannot set 'schedule'")
    channel = raw.get("channel")
    if channel != "email":
        raise LoopError(f"trigger[{i}]: channel must be 'email'")
    filters = raw.get("filters") or {}
    if not isinstance(filters, dict):
        raise LoopError(f"trigger[{i}]: filters must be an object")
    unknown = set(filters) - _CHANNEL_FILTER_ALLOWED_KEYS
    if unknown:
        raise LoopError(f"trigger[{i}]: unknown filter key(s) {sorted(unknown)}")
    for key, value in filters.items():
        if not isinstance(value, str) or not value.strip():
            raise LoopError(f"trigger[{i}]: filter {key!r} must be a non-empty string")
    semantic = raw.get("semantic")
    if semantic is not None and not (isinstance(semantic, str) and semantic.strip()):
        raise LoopError(f"trigger[{i}]: semantic must be a non-empty string if set")
    match = raw.get("match", "wake_or_new")
    if match not in _CHANNEL_MATCH_MODES:
        raise LoopError(f"trigger[{i}]: match must be one of {sorted(_CHANNEL_MATCH_MODES)}")
    return LoopTrigger(
        source="channel",
        channel=channel,
        filters=dict(filters),
        semantic=semantic,
        match=match,
    )


def parse_loop(data: dict) -> LoopSpec:
    if not isinstance(data, dict):
        raise LoopError("loop definition must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise LoopError("name must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    workflow = data.get("workflow")
    if not isinstance(workflow, str) or not workflow.strip():
        raise LoopError("workflow is required")
    goal = data.get("goal") or {}
    intent = goal.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        raise LoopError("goal.intent is required")
    checks = tuple(_parse_check(c, i) for i, c in enumerate(goal.get("checks") or []))
    checks_sufficient = bool(goal.get("checks_sufficient", False))
    if checks_sufficient:
        # Exists to make script-only loops zero-LLM: an assertion always needs
        # the judge, so checks_sufficient=True is incompatible with one. At
        # least one required check must exist, or there is no hard evidence
        # to declare the goal reached without asking the model.
        if any(c.kind == "assertion" for c in checks):
            raise LoopError("goal.checks_sufficient requires all checks to be scripts, not assertions")
        if not any(c.required for c in checks):
            raise LoopError("goal.checks_sufficient requires at least one required check")
    triggers = tuple(_parse_trigger(t, i) for i, t in enumerate(data.get("triggers") or []))
    concurrency = data.get("concurrency", "single")
    if concurrency not in ("single", "parallel"):
        raise LoopError("concurrency must be 'single' or 'parallel'")
    stuck_after = data.get("stuck_after", 3)
    if isinstance(stuck_after, bool) or not isinstance(stuck_after, int) or stuck_after < 1:
        raise LoopError("stuck_after must be an integer >= 1")
    operator_channel = data.get("operator_channel") or None
    operator_to = data.get("operator_to") or None
    return LoopSpec(
        name=name,
        workflow=workflow.strip(),
        goal_intent=intent.strip(),
        checks=checks,
        checks_sufficient=checks_sufficient,
        triggers=triggers,
        enabled=bool(data.get("enabled", True)),
        concurrency=concurrency,
        stuck_after=stuck_after,
        operator_channel=operator_channel,
        operator_to=operator_to,
    )


def _trigger_to_dict(t: LoopTrigger) -> dict:
    if t.source == "cron":
        return {"source": "cron", "schedule": t.schedule}
    entry = {"source": "channel", "channel": t.channel, "filters": t.filters, "match": t.match}
    if t.semantic is not None:
        entry["semantic"] = t.semantic
    return entry


def loop_to_dict(spec: LoopSpec) -> dict:
    return {
        "name": spec.name,
        "enabled": spec.enabled,
        "workflow": spec.workflow,
        "goal": {
            "intent": spec.goal_intent,
            "checks": [
                {k: v for k, v in {"kind": c.kind, "required": c.required, "command": c.command, "text": c.text}.items() if v is not None}
                for c in spec.checks
            ],
            "checks_sufficient": spec.checks_sufficient,
        },
        "triggers": [_trigger_to_dict(t) for t in spec.triggers],
        "concurrency": spec.concurrency,
        "stuck_after": spec.stuck_after,
        "operator_channel": spec.operator_channel,
        "operator_to": spec.operator_to,
    }
