"""CronService — list, remove, toggle, and trigger cron jobs.

Wraps durin's ``CronScheduler`` (``durin.cron.service.CronService``).  The
``run`` method validates whether a job can be started and returns the decision;
the actual ``asyncio.create_task`` spawn stays in the websocket shim (it is a
loop/connection concern that must not live in a service).

Extracted from ``durin/channels/websocket.py``
(``_handle_cron_list`` / ``_handle_cron_remove`` / ``_handle_cron_toggle`` /
``_handle_cron_run``) in SP1; the channel keeps wire-identical shims.
"""

from __future__ import annotations

from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    ForbiddenError,
    NotFoundError,
    Query,
    Result,
    UnavailableError,
    ValidationFailedError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _humanize_interval_ms(every_ms: int) -> str:
    """Render an ``every`` interval with the largest whole unit."""
    secs = every_ms // 1000
    if secs <= 0:
        return "0s"
    for unit_secs, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs % unit_secs == 0:
            return f"{secs // unit_secs}{suffix}"
    return f"{secs}s"


def _job_to_dict(job: Any, *, cron_scheduler: Any | None = None) -> dict[str, Any]:
    """Serialize a ``CronJob`` dataclass to the wire dict the webui expects.

    ``cron_scheduler`` is the *live* scheduler (may be ``None``); it is used
    only for the ``executing`` flag in the ``state`` block.
    """
    sched = job.schedule
    if sched.kind == "every":
        label = f"every {_humanize_interval_ms(sched.every_ms or 0)}"
    elif sched.kind == "cron":
        tz = f" ({sched.tz})" if sched.tz else ""
        label = f"{sched.expr}{tz}"
    elif sched.kind == "at":
        label = f"once at {sched.at_ms}"
    else:
        label = sched.kind
    is_system = job.payload.kind == "system_event"
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "is_system": is_system,
        "schedule": {
            "kind": sched.kind,
            "label": label,
            "expr": sched.expr,
            "every_ms": sched.every_ms,
            "at_ms": sched.at_ms,
            "tz": sched.tz,
        },
        "message": "" if is_system else job.payload.message,
        "channel": job.payload.channel or "",
        "state": {
            "next_run_at_ms": job.state.next_run_at_ms,
            "last_run_at_ms": job.state.last_run_at_ms,
            "last_status": job.state.last_status,
            "last_error": job.state.last_error,
            "executing": bool(
                cron_scheduler is not None
                and cron_scheduler.is_executing(job.id)
            ),
        },
        "created_at_ms": job.created_at_ms,
        "updated_at_ms": job.updated_at_ms,
    }


def _fresh_cron_scheduler():
    """Build a non-running CronScheduler bound to the workspace jobs.json.

    Read-only ops (``list_jobs``) work directly; mutating ops write to the
    action.jsonl log, which the gateway's running scheduler drains on its next
    tick.  Mirrors how SecretStore endpoints work.
    """
    from durin.config.loader import load_config
    from durin.cron.service import CronService as CronScheduler

    cfg = load_config()
    path = cfg.workspace_path / "cron" / "jobs.json"
    return CronScheduler(path)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class CronListQuery(Query):
    """No inputs — lists all jobs including disabled ones."""


class CronJobScheduleResult(Result):
    kind: str
    label: str
    expr: str | None
    every_ms: int | None
    at_ms: int | None
    tz: str | None


class CronJobStateResult(Result):
    next_run_at_ms: int | None
    last_run_at_ms: int | None
    last_status: str | None
    last_error: str | None
    executing: bool


class CronJobItem(Result):
    id: str
    name: str
    enabled: bool
    is_system: bool
    schedule: CronJobScheduleResult
    message: str
    channel: str
    state: CronJobStateResult
    created_at_ms: int
    updated_at_ms: int


class CronListResult(Result):
    jobs: list[CronJobItem]


class CronRemoveCommand(Command):
    id: str


class CronRemoveResult(Result):
    result: str


class CronRunCommand(Command):
    id: str


class CronRunResult(Result):
    started: bool
    reason: str | None = None


class CronToggleCommand(Command):
    id: str
    enabled: bool


class CronToggleResult(Result):
    job: CronJobItem


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CronService:
    """Read and mutate scheduled jobs.

    ``cron_scheduler`` is the *live* scheduler instance (from the gateway's
    ``_cron_service``).  It may be ``None`` when the service is constructed
    before the scheduler starts (e.g. tests).  Most methods create a fresh
    non-running ``CronScheduler`` for disk reads/writes; only ``run`` requires
    the live instance (to check ``is_executing`` and ``get_job``).
    """

    def __init__(self, cron_scheduler: Any | None = None) -> None:
        self._cron_scheduler = cron_scheduler

    @route(
        "GET",
        "/api/cron",
        scope=Scope.CRON_READ.value,
        request_model=CronListQuery,
        response_model=CronListResult,
        summary="List all scheduled jobs (including disabled and system jobs)",
    )
    async def list(self, query: CronListQuery, principal: Principal) -> CronListResult:
        principal.require(Scope.CRON_READ)
        cron = _fresh_cron_scheduler()
        jobs = cron.list_jobs(include_disabled=True)
        return CronListResult(
            jobs=[
                CronJobItem(**_job_to_dict(j, cron_scheduler=self._cron_scheduler))
                for j in jobs
            ]
        )

    @route(
        "GET",
        "/api/cron/remove",
        scope=Scope.CRON_WRITE.value,
        request_model=CronRemoveCommand,
        response_model=CronRemoveResult,
        summary="Remove a non-system cron job by id",
    )
    async def remove(self, cmd: CronRemoveCommand, principal: Principal) -> CronRemoveResult:
        principal.require(Scope.CRON_WRITE)
        cron = _fresh_cron_scheduler()
        result = cron.remove_job(cmd.id)
        if result == "not_found":
            raise NotFoundError("no such job", details={"id": cmd.id})
        if result == "protected":
            raise ForbiddenError("system job; cannot remove", details={"id": cmd.id})
        return CronRemoveResult(result=result)

    @route(
        "GET",
        "/api/cron/run",
        scope=Scope.CRON_WRITE.value,
        request_model=CronRunCommand,
        response_model=CronRunResult,
        summary="Manually trigger a cron job now (non-blocking)",
    )
    async def run(self, cmd: CronRunCommand, principal: Principal) -> CronRunResult:
        """Validate whether the job can run; background spawn stays in the shim.

        Raises ``UnavailableError`` when the live scheduler is absent,
        ``ValidationFailedError`` when ``id`` is empty,
        ``NotFoundError`` when the job does not exist.
        Returns ``CronRunResult(started=False, reason="already_running")`` when
        the job is already in flight.  On success, returns
        ``CronRunResult(started=True)``; the shim does the actual task spawn.
        """
        principal.require(Scope.CRON_WRITE)
        if self._cron_scheduler is None:
            raise UnavailableError("scheduler not available")
        if not cmd.id:
            raise ValidationFailedError("id is required")
        if self._cron_scheduler.get_job(cmd.id) is None:
            raise NotFoundError("no such job", details={"id": cmd.id})
        if self._cron_scheduler.is_executing(cmd.id):
            return CronRunResult(started=False, reason="already_running")
        return CronRunResult(started=True)

    @route(
        "GET",
        "/api/cron/toggle",
        scope=Scope.CRON_WRITE.value,
        request_model=CronToggleCommand,
        response_model=CronToggleResult,
        summary="Enable or disable a cron job without removing it",
    )
    async def toggle(self, cmd: CronToggleCommand, principal: Principal) -> CronToggleResult:
        principal.require(Scope.CRON_WRITE)
        cron = _fresh_cron_scheduler()
        job = cron.enable_job(cmd.id, enabled=cmd.enabled)
        if job is None:
            raise NotFoundError("no such job", details={"id": cmd.id})
        return CronToggleResult(
            job=CronJobItem(**_job_to_dict(job, cron_scheduler=self._cron_scheduler))
        )
