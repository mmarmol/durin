"""Keeps cron jobs in sync with loop trigger declarations. Jobs are
system-registered (idempotent across boots, refreshed on every sync), one
per trigger.

Note on removal: ``CronService.remove_job`` only protects jobs whose
``payload.kind == "system_event"`` — the memory_dream pattern. Loop trigger
jobs use ``payload.kind == "loop_trigger"``, so plain ``remove_job(job_id)``
works here; no bypass/force flag exists on the real signature.
"""

from __future__ import annotations

from durin.cron.types import CronJob, CronPayload, CronSchedule
from durin.loops.spec import LoopSpec
from durin.loops.store import list_loops

_PREFIX = "loop:"


def loop_job_id(loop_name: str, idx: int) -> str:
    return f"{_PREFIX}{loop_name}:{idx}"


def _existing_ids(cron_service, loop_name: str) -> list[str]:
    prefix = f"{_PREFIX}{loop_name}:"
    return [j.id for j in cron_service.list_jobs(include_disabled=True) if j.id.startswith(prefix)]


def sync_loop_jobs(cron_service, spec: LoopSpec) -> None:
    wanted: dict[str, CronJob] = {}
    if spec.enabled:
        for idx, trig in enumerate(spec.triggers):
            job_id = loop_job_id(spec.name, idx)
            wanted[job_id] = CronJob(
                id=job_id,
                name=f"loop {spec.name} trigger {idx}",
                schedule=CronSchedule(**trig.schedule),
                payload=CronPayload(kind="loop_trigger", loop=spec.name),
            )
    for job_id in _existing_ids(cron_service, spec.name):
        if job_id not in wanted:
            cron_service.remove_job(job_id)
    for job in wanted.values():
        cron_service.register_system_job(job)


def remove_loop_jobs(cron_service, loop_name: str) -> None:
    for job_id in _existing_ids(cron_service, loop_name):
        cron_service.remove_job(job_id)


def sync_all(cron_service, workspace) -> None:
    """Boot reconcile: sync every stored loop, then prune ``loop:*`` jobs
    whose loop no longer exists. Other system jobs (e.g. memory_dream) are
    untouched — this only looks at the ``loop:`` id prefix."""
    known: set[str] = set()
    for spec in list_loops(workspace):
        sync_loop_jobs(cron_service, spec)
        known.update(_existing_ids(cron_service, spec.name))
    for job in cron_service.list_jobs(include_disabled=True):
        if job.id.startswith(_PREFIX) and job.id not in known:
            cron_service.remove_job(job.id)
