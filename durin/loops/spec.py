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
    source: Literal["cron"]  # V1; channel/webhook sources arrive in V2/V4
    schedule: dict = field(default_factory=dict)  # CronSchedule-shaped: kind/at_ms/every_ms/expr/tz


@dataclass(frozen=True)
class LoopSpec:
    name: str
    workflow: str
    goal_intent: str
    checks: tuple[GoalCheck, ...] = ()
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
    if raw.get("source") != "cron":
        raise LoopError(f"trigger[{i}]: only source 'cron' is supported")
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
        triggers=triggers,
        enabled=bool(data.get("enabled", True)),
        concurrency=concurrency,
        stuck_after=stuck_after,
        operator_channel=operator_channel,
        operator_to=operator_to,
    )


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
        },
        "triggers": [{"source": t.source, "schedule": t.schedule} for t in spec.triggers],
        "concurrency": spec.concurrency,
        "stuck_after": spec.stuck_after,
        "operator_channel": spec.operator_channel,
        "operator_to": spec.operator_to,
    }
