"""PersonasService — list, upsert, and delete SOUL personality files.

Wraps ``SoulStore`` to expose the workspace's named souls over the HTTP API.
The default soul (``SOUL.md`` at the workspace root) can be read and overwritten
but not deleted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    ForbiddenError,
    Query,
    Result,
    ValidationFailedError,
)
from durin.souls.store import SoulStore


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class SoulListQuery(Query):
    """No inputs — lists all souls including the default."""


class SoulItem(Result):
    slug: str
    body: str


class SoulListResult(Result):
    souls: list[SoulItem]


class SoulUpsertCommand(Command):
    slug: str
    body: str


class SoulUpsertResult(Result):
    soul: SoulItem


class SoulDeleteCommand(Command):
    slug: str


class SoulDeleteResult(Result):
    ok: bool


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PersonasService:
    """Read and mutate SOUL personality files in the workspace."""

    def __init__(self, workspace_resolver: Callable[[], Path]) -> None:
        self._workspace = workspace_resolver

    def _store(self) -> SoulStore:
        return SoulStore(self._workspace())

    @route(
        "GET",
        "/api/v1/souls",
        scope=Scope.CONFIG_READ.value,
        request_model=SoulListQuery,
        response_model=SoulListResult,
        summary="List all SOUL personality files",
    )
    async def list_souls(self, query: SoulListQuery, principal: Principal) -> SoulListResult:
        principal.require(Scope.CONFIG_READ)
        store = self._store()
        return SoulListResult(
            souls=[SoulItem(slug=s, body=store.read(s)) for s in store.list()]
        )

    @route(
        "POST",
        "/api/v1/souls",
        scope=Scope.CONFIG_WRITE.value,
        request_model=SoulUpsertCommand,
        response_model=SoulUpsertResult,
        summary="Create or overwrite a SOUL personality file",
    )
    async def upsert_soul(self, cmd: SoulUpsertCommand, principal: Principal) -> SoulUpsertResult:
        principal.require(Scope.CONFIG_WRITE)
        store = self._store()
        try:
            store.write(cmd.slug, cmd.body)
        except ValueError as e:
            raise ValidationFailedError(str(e)) from e
        return SoulUpsertResult(soul=SoulItem(slug=cmd.slug, body=store.read(cmd.slug)))

    @route(
        "DELETE",
        "/api/v1/souls",
        scope=Scope.CONFIG_WRITE.value,
        request_model=SoulDeleteCommand,
        response_model=SoulDeleteResult,
        summary="Delete a named SOUL file (the default soul cannot be deleted)",
    )
    async def delete_soul(self, cmd: SoulDeleteCommand, principal: Principal) -> SoulDeleteResult:
        principal.require(Scope.CONFIG_WRITE)
        try:
            self._store().delete(cmd.slug)
        except ValueError as e:
            raise ForbiddenError(str(e)) from e
        return SoulDeleteResult(ok=True)
