"""Boot-time converter: loops/ definitions, run history, claims, queue, and
``loop:*`` cron jobs become their automations/ equivalents.

Runs once at gateway boot, before ``durin.automations.cron_sync.sync_all``
(see ``durin.cli.commands``) — wrapped in try/except there so a migration I/O
failure never fails gateway startup.

Parsing a pre-existing on-disk loop definition uses
``durin.automations._legacy_loop_spec.parse_loop`` — a frozen copy of the
parser the (now-deleted) loops package used, kept alive only for this one
read. Every converted definition is then hand-assembled as a plain dict and
run through ``durin.automations.spec.parse_automation`` (full validation) and
``durin.automations.store.save_automation`` (chain-cycle validation, normal
versioning) rather than built by copying an ``AutomationSpec`` together
in-process.

Idempotency: this only acts when ``<workspace>/loops/`` exists. A completed
migration renames it to ``loops-migrated/`` (kept around for rollback — the
original loop files, and anything left unmoved by the claims "already exists"
branch below, ride along in the rename), so a second call finds nothing to do
and returns ``[]`` without touching cron jobs or the filesystem again.

A loop that fails to parse or convert is skipped with a logged error and the
migration continues — this never fails the boot over one bad file. The
return value is a human-readable list of every action taken (or skipped),
for whatever calls this to log or surface.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from durin.automations._legacy_loop_spec import GoalCheck, LoopSpec, LoopTrigger, parse_loop
from durin.automations.run_log import runs_root as automations_runs_root
from durin.automations.spec import parse_automation
from durin.automations.store import automations_dir, save_automation

# Legacy loop cron job id prefix (the deleted loops package's own cron_sync
# used the format f"loop:{loop_name}:{idx}") — matched here as a plain string
# so this module never needs to import anything from the loops package.
_LOOP_CRON_PREFIX = "loop:"


def _convert_trigger(trig: LoopTrigger, *, loop_name: str, workflow: str, actions: list[str]) -> dict:
    if trig.source == "cron":
        # The automations spec requires a schedule trigger's `task` text;
        # loops never had one. Synthesize it and say so — this is a real,
        # user-visible change to what fires, not a mechanical rename.
        task = f"Run the {workflow} workflow"
        note = f"loop '{loop_name}': synthesized schedule task {task!r} for its cron trigger (loops had no task text)"
        logger.info("migrate: {}", note)
        actions.append(note)
        return {"source": "schedule", "schedule": dict(trig.schedule), "task": task}
    if trig.source == "webhook":
        entry: dict = {"source": "webhook", "hook": trig.hook}
        if trig.semantic is not None:
            entry["semantic"] = trig.semantic
        if trig.correlate is not None:
            entry["correlate"] = trig.correlate
        return entry
    # channel — verbatim, filters/semantic/correlate/match all carry over.
    entry = {"source": "channel", "channel": trig.channel, "filters": dict(trig.filters), "match": trig.match}
    if trig.semantic is not None:
        entry["semantic"] = trig.semantic
    if trig.correlate is not None:
        entry["correlate"] = trig.correlate
    return entry


def _warn_dropped_check(loop_spec: LoopSpec, i: int, check: GoalCheck, actions: list[str]) -> None:
    label = check.command if check.kind == "script" else check.text
    note = (
        f"loop '{loop_spec.name}' goal check[{i}] ({check.kind} {label!r}) dropped — "
        "a check's pass/fail verdict cannot feed life.achieved_when; add a final exit-0 "
        "labeler `cases` node emitting ACHIEVED / NOT_ACHIEVED to the workflow "
        f"'{loop_spec.workflow}' instead"
    )
    logger.warning("migrate: {}", note)
    actions.append(note)


def _is_multicase(loop_spec: LoopSpec) -> bool:
    """Whether this loop serves many independent cases at once.

    A loop's goal was judged per RUN: each ticket/case run checked its own
    completion and the loop stayed armed for the next one. An automation's
    life belongs to the AUTOMATION: `achieved_when: "any_completed"` disables
    every trigger after the first completed run. Mapping a multi-case loop's
    goal onto a life therefore kills the standing pipeline the moment it
    first succeeds — silently, since disabling is the life feature working
    as designed. The multi-case signals are a `correlate` pattern on any
    trigger (each captured id is its own case) or parallel concurrency
    (several cases in flight at once).
    """
    return loop_spec.concurrency == "parallel" or any(
        t.correlate is not None for t in loop_spec.triggers
    )


def _convert_loop(loop_spec: LoopSpec, actions: list[str]) -> dict:
    for i, check in enumerate(loop_spec.checks):
        _warn_dropped_check(loop_spec, i, check, actions)
    life: dict | None = {
        "intent": loop_spec.goal_intent,
        "achieved_when": "any_completed",
        "max_attempts": loop_spec.stuck_after,
        "on_stuck": "notify",
    }
    if _is_multicase(loop_spec):
        note = (
            f"loop '{loop_spec.name}' goal not migrated to a life condition — the loop "
            "serves many cases (correlate pattern and/or parallel concurrency), and a "
            'life with achieved_when "any_completed" would disable the automation after '
            "its first completed run; the automation stays standing with no life. Add a "
            "life back only for a single-case automation"
        )
        logger.warning("migrate: {}", note)
        actions.append(note)
        life = None
    converted = {
        "name": loop_spec.name,
        "workflow": loop_spec.workflow,
        "enabled": loop_spec.enabled,
        "concurrency": loop_spec.concurrency,
        "triggers": [
            _convert_trigger(t, loop_name=loop_spec.name, workflow=loop_spec.workflow, actions=actions)
            for t in loop_spec.triggers
        ],
        "life": life,
        "delivery": {
            "channel": loop_spec.operator_channel,
            "to": loop_spec.operator_to,
            "notify": "failures_only",
        },
        "help": {
            "channel": loop_spec.operator_channel,
            "to": loop_spec.operator_to,
        },
    }
    if life is None:
        del converted["life"]
    return converted


def _convert_definitions(workspace: Path, loops_dir: Path, actions: list[str]) -> None:
    for p in sorted(loops_dir.glob("*.json")):
        if p.name == "claims.json":
            continue  # not a loop definition — the claims index lives beside them (see _move_claims)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            loop_spec = parse_loop(raw)
        except Exception as exc:  # noqa: BLE001 — one bad file must never abort the boot
            logger.error("migrate: skipping unparseable loop file {}: {}", p, exc)
            actions.append(f"skipped {p.name}: {exc}")
            continue
        try:
            automation_data = _convert_loop(loop_spec, actions)
            automation_spec = parse_automation(automation_data)
            save_automation(workspace, automation_spec, actor="system", reason="migrated from loop")
        except Exception as exc:  # noqa: BLE001 — a conversion failure must never abort the boot
            logger.error("migrate: skipping loop '{}' — conversion failed: {}", loop_spec.name, exc)
            actions.append(f"skipped {loop_spec.name}: {exc}")
            continue
        actions.append(f"migrated loop '{loop_spec.name}' -> automation '{loop_spec.name}'")


def _move_runs(workspace: Path, actions: list[str]) -> None:
    # Mirrors the deleted loops package's own run_log.runs_root, not imported
    # (see module docstring).
    src = workspace / "loops-runs"
    if not src.is_dir():
        return
    dst = automations_runs_root(workspace)
    src.rename(dst)
    actions.append(f"moved {src} to {dst}")


def _move_claims(workspace: Path, loops_dir: Path, actions: list[str]) -> None:
    src = loops_dir / "claims.json"
    if not src.is_file():
        return
    dst = automations_dir(workspace) / "claims.json"
    if dst.exists():
        note = "automations/claims.json already exists — left loops/claims.json in place"
        logger.info("migrate: {}", note)
        actions.append(note)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    actions.append(f"moved {src} to {dst}")


def _move_queue(workspace: Path, loops_dir: Path, actions: list[str]) -> None:
    src = loops_dir / "queue"
    if not src.is_dir():
        return
    dst = automations_dir(workspace) / "queue"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    actions.append(f"moved {src} to {dst}")


def _prune_loop_cron_jobs(cron_service, actions: list[str]) -> None:
    if cron_service is None:
        note = "skipped loop:* cron job prune (no cron_service given)"
        logger.info("migrate: {}", note)
        actions.append(note)
        return
    for job in cron_service.list_jobs(include_disabled=True):
        if job.id.startswith(_LOOP_CRON_PREFIX):
            cron_service.remove_job(job.id)
            actions.append(f"removed cron job {job.id}")


def migrate_loops(workspace: Path, cron_service=None) -> list[str]:
    """Convert every ``loops/<name>.json`` into an ``automations/<name>.json``,
    relocate loops' run history/claims/queue, rename ``loops/`` out of the way,
    and prune legacy ``loop:*`` cron jobs. Idempotent: a workspace with no
    ``loops/`` directory (never had one, or already migrated) no-ops and
    returns ``[]``. Returns the human-readable list of actions taken."""
    workspace = Path(workspace)
    loops_dir = workspace / "loops"
    actions: list[str] = []

    if not loops_dir.is_dir():
        return actions

    _convert_definitions(workspace, loops_dir, actions)
    _move_runs(workspace, actions)
    _move_claims(workspace, loops_dir, actions)
    _move_queue(workspace, loops_dir, actions)

    migrated_dir = workspace / "loops-migrated"
    loops_dir.rename(migrated_dir)
    actions.append(f"renamed {loops_dir} to {migrated_dir}")

    _prune_loop_cron_jobs(cron_service, actions)

    return actions
