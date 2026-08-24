"""Keeps cron jobs in sync with automation trigger declarations. Jobs are
system-registered (idempotent across boots, refreshed on every sync), one
per enabled ``schedule`` trigger.

Ported from ``durin/loops/cron_sync.py`` (``durin/loops/`` stays untouched
and importable until a later gateway-wiring task cuts over to this module
and retires it entirely — deleting it now would break the still-live loops
package mid-migration). The behavior here adds a wrinkle the loops version
never needed: ``CronService.remove_job`` protects ``automation_trigger``
jobs from the public API (they are owned by an automation, not a
user-editable cron entry — mirroring how ``system_event`` jobs are
protected). This module's own removal paths
(``sync_automation_jobs``'s stale-id cleanup, ``remove_automation_jobs``,
and ``sync_all``'s orphan sweep) therefore use
``CronService.remove_system_job`` — the bypass door ``register_system_job``
already relies on for writes — instead of ``remove_job``.

``sync_all`` ALSO prunes every legacy ``loop:*`` cron job, unconditionally,
regardless of whether a same-named loop or automation still exists. This is
the self-healing backstop for the eventual loops cutover: once the
gateway's ``loop_trigger`` dispatch branch is retired, a surviving
``loop:*`` job would fire an empty agent turn on its next tick with no
handler for it. The boot migration (``durin.automations.migrate``) also
prunes ``loop:*`` jobs on the same occasion — this is deliberate
belt-and-suspenders, not a conflict; both removals are idempotent.

IMPORTANT — not wired yet: this module is built and tested standalone.
``sync_all`` is destructive toward loops' own cron jobs, so it must NOT be
called from anywhere until the gateway cutover task rewires
``durin/cli/commands.py`` to call it in place of (not alongside) loops'
``sync_all``. Until that lands, the gateway continues to run
``durin.loops.cron_sync.sync_all`` at boot as today.
"""

from __future__ import annotations

from durin.automations.spec import AutomationSpec
from durin.automations.store import list_automations
from durin.cron.types import CronJob, CronPayload, CronSchedule

_PREFIX = "automation:"
# Legacy loop job id prefix (durin.loops.cron_sync.loop_job_id's format).
# Not imported from durin.loops — that package is deleted at cutover and this
# module must keep working after it is gone.
_LEGACY_LOOP_PREFIX = "loop:"


def automation_job_id(name: str, idx: int) -> str:
    return f"{_PREFIX}{name}:{idx}"


def _existing_ids(cron_service, name: str) -> list[str]:
    prefix = f"{_PREFIX}{name}:"
    return [j.id for j in cron_service.list_jobs(include_disabled=True) if j.id.startswith(prefix)]


def sync_automation_jobs(cron_service, spec: AutomationSpec) -> None:
    wanted: dict[str, CronJob] = {}
    if spec.enabled:
        for idx, trig in enumerate(spec.triggers):
            if trig.source != "schedule":
                continue
            job_id = automation_job_id(spec.name, idx)
            wanted[job_id] = CronJob(
                id=job_id,
                name=f"automation {spec.name} trigger {idx}",
                schedule=CronSchedule(**trig.schedule),
                payload=CronPayload(kind="automation_trigger", automation=spec.name, message=trig.task),
            )
    for job_id in _existing_ids(cron_service, spec.name):
        if job_id not in wanted:
            cron_service.remove_system_job(job_id)
    for job in wanted.values():
        cron_service.register_system_job(job)


def remove_automation_jobs(cron_service, name: str) -> None:
    for job_id in _existing_ids(cron_service, name):
        cron_service.remove_system_job(job_id)


def sync_all(cron_service, workspace) -> None:
    """Boot reconcile: sync every stored automation, then prune orphaned
    ``automation:*`` jobs (owning automation no longer exists) and every
    legacy ``loop:*`` job, unconditionally. See the module docstring for why
    the legacy prune is unconditional and why this is not wired in yet."""
    known: set[str] = set()
    for spec in list_automations(workspace):
        sync_automation_jobs(cron_service, spec)
        known.update(_existing_ids(cron_service, spec.name))
    for job in cron_service.list_jobs(include_disabled=True):
        if job.id.startswith(_PREFIX) and job.id not in known:
            cron_service.remove_system_job(job.id)
        elif job.id.startswith(_LEGACY_LOOP_PREFIX):
            cron_service.remove_system_job(job.id)
