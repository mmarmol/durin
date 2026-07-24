"""LoopsService — list, load, save, fire, and answer loop definitions.

Loops live as JSON at ``<workspace>/loops/<name>.json`` (see
``durin.loops.store``) and are validated by ``durin.loops.spec.parse_loop``.
This is the HTTP surface the webui loops view uses to manage loops and drive
manual fires / operator answers. A save or delete keeps the loop's cron
trigger jobs in sync via ``durin.loops.cron_sync``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from durin.loops import queue, run_log
from durin.loops.cron_sync import remove_loop_jobs, sync_loop_jobs
from durin.loops.runtime import LoopBusy
from durin.loops.spec import LoopError, LoopNotFound, loop_to_dict, parse_loop
from durin.loops.store import delete_loop, list_loops, load_loop, save_loop
from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    NotFoundError,
    Query,
    Result,
    UnavailableError,
    ValidationFailedError,
)


class LoopsListQuery(Query):
    """No inputs — lists every loop, each with its live run counts."""


class LoopsListResult(Result):
    loops: list[dict[str, Any]]   # loop_to_dict() fields + active_runs/needs_operator/waiting_info/pending_events counts


class LoopGetQuery(Query):
    name: str


class LoopGetResult(Result):
    name: str
    definition: dict[str, Any]   # loop_to_dict() shape


class LoopSaveCommand(Command):
    name: str
    definition: dict[str, Any]


class LoopSaveResult(Result):
    name: str


class LoopDeleteCommand(Command):
    name: str


class LoopDeleteResult(Result):
    deleted: bool


class LoopFireCommand(Command):
    name: str
    task: str = ""


class LoopFireResult(Result):
    run: dict[str, Any]   # the run manifest record (durin.loops.run_log shape)


class LoopAnswerCommand(Command):
    name: str
    run_id: str
    answer: str


class LoopAnswerResult(Result):
    run: dict[str, Any]


class LoopRunsQuery(Query):
    name: str
    limit: int = 50


class LoopRunsResult(Result):
    runs: list[dict[str, Any]]   # newest-first manifests for this loop


class LoopsRunsQuery(Query):
    limit: int = 50


class LoopsRunsResult(Result):
    runs: list[dict[str, Any]]   # newest-first manifests across every loop


class LoopStatsQuery(Query):
    name: str


class LoopStatsResult(Result):
    name: str
    outcomes: list[dict[str, Any]]   # last 20 terminal runs, newest-first: {run_id, status, goal_reached, started_at, finished_at}
    convergence: float | None        # done / terminal over ALL retained runs; None when no terminal runs
    escalation_rate: float | None    # escalated / terminal over ALL retained runs; None when no terminal runs
    counts: dict[str, int]           # {running, needs_operator, waiting_info, done, no_goal, escalated, error}
    pending_events: int


class LoopsHooksSecretQuery(Query):
    """No inputs — returns the shared webhook ingress secret."""


class LoopsHooksSecretResult(Result):
    secret: str
    path_template: str   # "/api/v1/hooks/{hook}" — the caller substitutes {hook}


_TERMINAL_STATUSES = ("done", "no_goal", "escalated", "error")
_ALL_STATUSES = run_log.ACTIVE_STATUSES + _TERMINAL_STATUSES
_OUTCOMES_LIMIT = 20


def _counts(workspace: Path, name: str) -> dict[str, int]:
    active = run_log.active_runs(workspace, name)
    needs_operator = sum(1 for r in active if r.get("status") == "needs_operator")
    waiting_info = sum(1 for r in active if r.get("status") == "waiting_info")
    return {
        "active_runs": len(active),
        "needs_operator": needs_operator,
        "waiting_info": waiting_info,
        "pending_events": queue.pending(workspace, name),
    }


def _stats(workspace: Path, name: str) -> dict[str, Any]:
    runs = run_log.list_runs(workspace, name, limit=None)   # newest-first
    counts = {status: 0 for status in _ALL_STATUSES}
    for r in runs:
        status = r.get("status")
        if status in counts:
            counts[status] += 1
    terminal = sum(counts[s] for s in _TERMINAL_STATUSES)
    convergence = counts["done"] / terminal if terminal else None
    escalation_rate = counts["escalated"] / terminal if terminal else None
    outcomes = [
        {
            "run_id": r.get("run_id"), "status": r.get("status"),
            "goal_reached": r.get("goal_reached"),
            "started_at": r.get("started_at"), "finished_at": r.get("finished_at"),
        }
        for r in runs if r.get("status") in _TERMINAL_STATUSES
    ][:_OUTCOMES_LIMIT]
    return {
        "outcomes": outcomes,
        "convergence": convergence,
        "escalation_rate": escalation_rate,
        "counts": counts,
        "pending_events": queue.pending(workspace, name),
    }


class LoopsService:
    def __init__(
        self, workspace: Path, cron_service: Any = None, runtime: Any = None,
        hooks_secret: Callable[[], str] | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._cron_service = cron_service   # durin.cron.service.CronService — keeps trigger jobs in sync
        self._runtime = runtime             # durin.loops.runtime.LoopsRuntime — None on surfaces without one
        self._hooks_secret = hooks_secret   # () -> str, e.g. ApiTokenStore().get_or_create_hooks_secret

    @route(
        "GET", "/api/v1/loops",
        scope=Scope.LOOPS_READ.value,
        request_model=LoopsListQuery, response_model=LoopsListResult,
        summary="List all loop definitions, with live run counts.",
    )
    async def list(self, query: LoopsListQuery, principal: Principal) -> LoopsListResult:
        principal.require(Scope.LOOPS_READ)
        loops = []
        for spec in list_loops(self._workspace):
            loops.append({**loop_to_dict(spec), **_counts(self._workspace, spec.name)})
        return LoopsListResult(loops=loops)

    @route(
        "GET", "/api/v1/loops/{name}",
        scope=Scope.LOOPS_READ.value,
        request_model=LoopGetQuery, response_model=LoopGetResult,
        summary="Load one loop's full definition.",
    )
    async def get(self, query: LoopGetQuery, principal: Principal) -> LoopGetResult:
        principal.require(Scope.LOOPS_READ)
        try:
            spec = load_loop(self._workspace, query.name)
        except LoopNotFound:
            raise NotFoundError(f"loop {query.name!r} not found")
        return LoopGetResult(name=spec.name, definition=loop_to_dict(spec))

    @route(
        "PUT", "/api/v1/loops/{name}",
        scope=Scope.LOOPS_WRITE.value,
        request_model=LoopSaveCommand, response_model=LoopSaveResult,
        summary="Create or update a loop definition.",
    )
    async def save(self, cmd: LoopSaveCommand, principal: Principal) -> LoopSaveResult:
        principal.require(Scope.LOOPS_WRITE)
        # The URL is authoritative for identity, same precedent as
        # WorkflowsService.duplicate() overwriting the inner "name" field.
        definition = {**cmd.definition, "name": cmd.name}
        try:
            spec = parse_loop(definition)
        except LoopError as exc:
            raise ValidationFailedError(f"invalid loop: {exc}")
        save_loop(self._workspace, spec, actor="user", reason="saved in the loops editor")
        sync_loop_jobs(self._cron_service, spec)
        return LoopSaveResult(name=spec.name)

    @route(
        "DELETE", "/api/v1/loops/{name}",
        scope=Scope.LOOPS_WRITE.value,
        request_model=LoopDeleteCommand, response_model=LoopDeleteResult,
        summary="Delete a loop definition.",
    )
    async def delete(self, cmd: LoopDeleteCommand, principal: Principal) -> LoopDeleteResult:
        principal.require(Scope.LOOPS_WRITE)
        try:
            delete_loop(self._workspace, cmd.name, actor="user",
                        reason="deleted in the loops editor")
        except LoopNotFound:
            raise NotFoundError(f"loop {cmd.name!r} not found")
        remove_loop_jobs(self._cron_service, cmd.name)
        return LoopDeleteResult(deleted=True)

    @route(
        "POST", "/api/v1/loops/{name}/fire",
        scope=Scope.LOOPS_WRITE.value,
        request_model=LoopFireCommand, response_model=LoopFireResult,
        summary="Manually fire a loop.",
    )
    async def fire(self, cmd: LoopFireCommand, principal: Principal) -> LoopFireResult:
        principal.require(Scope.LOOPS_WRITE)
        if self._runtime is None:
            raise UnavailableError("firing a loop is not available on this surface")
        try:
            record = await self._runtime.fire(cmd.name, source="manual", task=cmd.task or None)
        except LoopBusy as exc:
            raise ValidationFailedError(f"loop busy: {exc}")
        except LoopNotFound as exc:
            raise NotFoundError(str(exc))
        return LoopFireResult(run=record)

    @route(
        "POST", "/api/v1/loops/{name}/runs/{run_id}/answer",
        scope=Scope.LOOPS_WRITE.value,
        request_model=LoopAnswerCommand, response_model=LoopAnswerResult,
        summary="Answer a loop run awaiting an operator or a counterpart reply.",
    )
    async def answer(self, cmd: LoopAnswerCommand, principal: Principal) -> LoopAnswerResult:
        principal.require(Scope.LOOPS_WRITE)
        if self._runtime is None:
            raise UnavailableError("answering a loop run is not available on this surface")
        try:
            record = await self._runtime.answer(cmd.name, cmd.run_id, cmd.answer)
        except LoopNotFound as exc:
            raise NotFoundError(str(exc))
        except ValueError as exc:
            raise ValidationFailedError(str(exc))
        return LoopAnswerResult(run=record)

    @route(
        "GET", "/api/v1/loops/runs",
        scope=Scope.LOOPS_READ.value,
        request_model=LoopsRunsQuery, response_model=LoopsRunsResult,
        summary="Global activity feed across every loop, newest-first.",
    )
    async def runs_feed(self, query: LoopsRunsQuery, principal: Principal) -> LoopsRunsResult:
        principal.require(Scope.LOOPS_READ)
        return LoopsRunsResult(runs=run_log.list_all_runs(self._workspace, query.limit))

    @route(
        "GET", "/api/v1/loops/{name}/runs",
        scope=Scope.LOOPS_READ.value,
        request_model=LoopRunsQuery, response_model=LoopRunsResult,
        summary="List one loop's persisted runs, newest-first.",
    )
    async def runs_list(self, query: LoopRunsQuery, principal: Principal) -> LoopRunsResult:
        principal.require(Scope.LOOPS_READ)
        return LoopRunsResult(runs=run_log.list_runs(self._workspace, query.name, query.limit))

    @route(
        "GET", "/api/v1/loops/{name}/stats",
        scope=Scope.LOOPS_READ.value,
        request_model=LoopStatsQuery, response_model=LoopStatsResult,
        summary="Outcome stats for one loop: recent terminal runs, convergence, escalation rate.",
    )
    async def stats(self, query: LoopStatsQuery, principal: Principal) -> LoopStatsResult:
        principal.require(Scope.LOOPS_READ)
        try:
            load_loop(self._workspace, query.name)
        except LoopNotFound:
            raise NotFoundError(f"loop {query.name!r} not found")
        return LoopStatsResult(name=query.name, **_stats(self._workspace, query.name))

    @route(
        "GET", "/api/v1/loops/hooks-secret",
        scope=Scope.LOOPS_WRITE.value,
        request_model=LoopsHooksSecretQuery, response_model=LoopsHooksSecretResult,
        summary="Return the shared webhook ingress secret and its path template.",
    )
    async def hooks_secret(self, query: LoopsHooksSecretQuery, principal: Principal) -> LoopsHooksSecretResult:
        principal.require(Scope.LOOPS_WRITE)
        if self._hooks_secret is None:
            raise UnavailableError("the webhook ingress secret is not available on this surface")
        return LoopsHooksSecretResult(secret=self._hooks_secret(), path_template="/api/v1/hooks/{hook}")
