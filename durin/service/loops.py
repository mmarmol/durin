"""LoopsService — list, load, save, fire, and answer loop definitions.

Loops live as JSON at ``<workspace>/loops/<name>.json`` (see
``durin.loops.store``) and are validated by ``durin.loops.spec.parse_loop``.
This is the HTTP surface the webui loops view uses to manage loops and drive
manual fires / operator answers. A save or delete keeps the loop's cron
trigger jobs in sync via ``durin.loops.cron_sync``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from durin.loops import run_log
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
    loops: list[dict[str, Any]]   # loop_to_dict() fields + active_runs/needs_operator counts


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


def _counts(workspace: Path, name: str) -> tuple[int, int]:
    active = run_log.active_runs(workspace, name)
    needs_operator = sum(1 for r in active if r.get("status") == "needs_operator")
    return len(active), needs_operator


class LoopsService:
    def __init__(self, workspace: Path, cron_service: Any = None, runtime: Any = None) -> None:
        self._workspace = Path(workspace)
        self._cron_service = cron_service   # durin.cron.service.CronService — keeps trigger jobs in sync
        self._runtime = runtime             # durin.loops.runtime.LoopsRuntime — None on surfaces without one

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
            active_runs, needs_operator = _counts(self._workspace, spec.name)
            loops.append({**loop_to_dict(spec), "active_runs": active_runs, "needs_operator": needs_operator})
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
        save_loop(self._workspace, spec)
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
            delete_loop(self._workspace, cmd.name)
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
        summary="Answer a loop run that is waiting on an operator.",
    )
    async def answer(self, cmd: LoopAnswerCommand, principal: Principal) -> LoopAnswerResult:
        principal.require(Scope.LOOPS_WRITE)
        if self._runtime is None:
            raise UnavailableError("answering a loop run is not available on this surface")
        try:
            record = await self._runtime.answer(cmd.name, cmd.run_id, cmd.answer)
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
