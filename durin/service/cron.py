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
        "mode": job.payload.mode,
        "model": job.payload.model,
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
        "run_history": [] if is_system else [
            {
                "run_at_ms": r.run_at_ms,
                "status": r.status,
                "duration_ms": r.duration_ms,
                "error": r.error,
                "session_key": r.session_key,
                "model": r.model,
                "summary": r.summary,
            }
            for r in job.state.run_history
        ],
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


_VALID_SCHEDULE_KINDS = {"cron", "every", "at"}


def _schedule_from_cmd(cmd: Any) -> Any:
    """Build a CronSchedule from command fields (schedule_kind/expr/every_ms/at_ms/tz).

    Rejects a ``schedule_kind`` outside ``{cron, every, at}`` loudly: an
    unknown kind (e.g. the legacy webui "interval") otherwise falls through
    ``_compute_next_run`` to ``None``, so the job is created (HTTP 200) but
    never fires.
    """
    from durin.cron.types import CronSchedule

    if cmd.schedule_kind not in _VALID_SCHEDULE_KINDS:
        raise ValidationFailedError(
            f"invalid schedule_kind '{cmd.schedule_kind}'",
            details={"allowed": sorted(_VALID_SCHEDULE_KINDS)},
        )
    return CronSchedule(
        kind=cmd.schedule_kind,
        expr=cmd.expr,
        every_ms=cmd.every_ms,
        at_ms=cmd.at_ms,
        tz=cmd.tz,
    )


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


class CronRunRecordResult(Result):
    run_at_ms: int
    status: str
    duration_ms: int
    error: str | None = None
    session_key: str | None = None
    model: str | None = None
    summary: str | None = None


class CronJobItem(Result):
    id: str
    name: str
    enabled: bool
    is_system: bool
    schedule: CronJobScheduleResult
    message: str
    mode: str
    model: str | None = None
    channel: str
    state: CronJobStateResult
    run_history: list[CronRunRecordResult] = []
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


class CronAddCommand(Command):
    name: str
    message: str
    mode: str = "reminder"
    model: str | None = None
    schedule_kind: str
    expr: str | None = None
    every_ms: int | None = None
    at_ms: int | None = None
    tz: str | None = None
    deliver: bool = False
    channel: str | None = None
    to: str | None = None


class CronUpdateCommand(Command):
    id: str
    name: str | None = None
    message: str | None = None
    mode: str | None = None
    model: str | None = None
    schedule_kind: str | None = None
    expr: str | None = None
    every_ms: int | None = None
    at_ms: int | None = None
    tz: str | None = None
    deliver: bool | None = None
    channel: str | None = None
    to: str | None = None


class CronAddResult(Result):
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
        "/api/v1/cron",
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
        "DELETE",
        "/api/v1/cron",
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
        "POST",
        "/api/v1/cron/run",
        scope=Scope.CRON_WRITE.value,
        request_model=CronRunCommand,
        response_model=CronRunResult,
        summary="Manually trigger a cron job now (non-blocking)",
    )
    async def run(self, cmd: CronRunCommand, principal: Principal) -> CronRunResult:
        """Validate the job and spawn it immediately as a background task.

        Raises ``UnavailableError`` when the live scheduler is absent,
        ``ValidationFailedError`` when ``id`` is empty,
        ``NotFoundError`` when the job does not exist.
        Returns ``CronRunResult(started=False, reason="already_running")`` when
        the job is already in flight.  On success, spawns
        ``run_job(force=True)`` as a background task (overlap-guarded by
        ``_executing``) and returns ``CronRunResult(started=True)``.
        """
        import asyncio

        principal.require(Scope.CRON_WRITE)
        if self._cron_scheduler is None:
            raise UnavailableError("scheduler not available")
        if not cmd.id:
            raise ValidationFailedError("id is required")
        if self._cron_scheduler.get_job(cmd.id) is None:
            raise NotFoundError("no such job", details={"id": cmd.id})
        if self._cron_scheduler.is_executing(cmd.id):
            return CronRunResult(started=False, reason="already_running")
        asyncio.create_task(self._cron_scheduler.run_job(cmd.id, force=True))
        return CronRunResult(started=True)

    @route(
        "POST",
        "/api/v1/cron/toggle",
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

    @route(
        "POST",
        "/api/v1/cron",
        scope=Scope.CRON_WRITE.value,
        request_model=CronAddCommand,
        response_model=CronAddResult,
        summary="Create a new agent_turn cron job",
    )
    async def create(self, cmd: CronAddCommand, principal: Principal) -> CronAddResult:
        principal.require(Scope.CRON_WRITE)
        cron = _fresh_cron_scheduler()
        schedule = _schedule_from_cmd(cmd)
        job = cron.add_job(
            name=cmd.name,
            schedule=schedule,
            message=cmd.message,
            deliver=cmd.deliver,
            channel=cmd.channel,
            to=cmd.to,
            mode=cmd.mode,
            model=cmd.model,
        )
        return CronAddResult(
            job=CronJobItem(**_job_to_dict(job, cron_scheduler=self._cron_scheduler))
        )

    @route(
        "PATCH",
        "/api/v1/cron",
        scope=Scope.CRON_WRITE.value,
        request_model=CronUpdateCommand,
        response_model=CronToggleResult,
        summary="Update a non-system cron job",
    )
    async def update(self, cmd: CronUpdateCommand, principal: Principal) -> CronToggleResult:
        principal.require(Scope.CRON_WRITE)
        cron = _fresh_cron_scheduler()
        schedule = _schedule_from_cmd(cmd) if cmd.schedule_kind else None
        result = cron.update_job(
            cmd.id,
            name=cmd.name,
            schedule=schedule,
            message=cmd.message,
            deliver=cmd.deliver,
            mode=cmd.mode,
            model=(cmd.model if cmd.model is not None else ...),
            channel=(cmd.channel if cmd.channel is not None else ...),
            to=(cmd.to if cmd.to is not None else ...),
        )
        if result == "not_found":
            raise NotFoundError("no such job", details={"id": cmd.id})
        if result == "protected":
            raise ForbiddenError("system job; cannot update", details={"id": cmd.id})
        return CronToggleResult(
            job=CronJobItem(**_job_to_dict(result, cron_scheduler=self._cron_scheduler))
        )
