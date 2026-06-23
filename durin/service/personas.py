"""PersonasService — SOUL personality files + persona config CRUD.

Two surfaces:
- SOUL files: list, upsert, delete named SOUL.md personality files in the workspace.
- Personas: list, upsert, delete named PersonaConfig entries in config (user personas
  and built-ins); set or clear the global default persona.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from durin.config.loader import get_config_path, load_config, mutate_config
from durin.config.schema import PersonaConfig
from durin.personas.builtin import BUILTIN_PERSONAS
from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    ForbiddenError,
    NotFoundError,
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
# Persona DTOs
# ---------------------------------------------------------------------------


class PersonaListQuery(Query):
    """No inputs — lists all personas (user + built-in) and the default."""


class PersonaItem(Result):
    name: str
    soul: str
    model: str | None = None
    description: str | None = None
    builtin: bool = False


class PersonaListResult(Result):
    personas: list[PersonaItem]
    default: str | None = None


class PersonaUpsertCommand(Command):
    name: str
    soul: str = "default"
    model: str | None = None
    description: str | None = None


class PersonaUpsertResult(Result):
    persona: PersonaItem


class PersonaDeleteCommand(Command):
    name: str


class PersonaDeleteResult(Result):
    ok: bool


class SetDefaultPersonaCommand(Command):
    name: str | None = None


class SetDefaultPersonaResult(Result):
    default: str | None = None


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
        store = self._store()
        try:
            store.delete(cmd.slug)
        except ValueError as e:
            msg = str(e)
            if "invalid soul slug" in msg:
                raise ValidationFailedError(msg) from e
            raise ForbiddenError(msg) from e
        return SoulDeleteResult(ok=True)

    # ------------------------------------------------------------------
    # Persona config endpoints
    # ------------------------------------------------------------------

    @route(
        "GET",
        "/api/v1/personas",
        scope=Scope.CONFIG_READ.value,
        request_model=PersonaListQuery,
        response_model=PersonaListResult,
        summary="List personas (user + built-in) and the default",
    )
    async def list_personas(self, query: PersonaListQuery, principal: Principal) -> PersonaListResult:
        principal.require(Scope.CONFIG_READ)
        cfg = load_config(get_config_path())
        items: list[PersonaItem] = []
        for name, p in cfg.personas.items():
            items.append(PersonaItem(name=name, soul=p.soul, model=p.model, description=p.description, builtin=False))
        for name, p in BUILTIN_PERSONAS.items():
            if name not in cfg.personas:
                items.append(PersonaItem(name=name, soul=p.soul, model=p.model, description=p.description, builtin=True))
        items.sort(key=lambda i: i.name)
        return PersonaListResult(personas=items, default=cfg.agents.defaults.persona)

    @route(
        "POST",
        "/api/v1/personas",
        scope=Scope.CONFIG_WRITE.value,
        request_model=PersonaUpsertCommand,
        response_model=PersonaUpsertResult,
        summary="Create or update a user persona",
    )
    async def upsert_persona(self, cmd: PersonaUpsertCommand, principal: Principal) -> PersonaUpsertResult:
        principal.require(Scope.CONFIG_WRITE)

        def _m(cfg: object) -> None:
            cfg.personas[cmd.name] = PersonaConfig(soul=cmd.soul, model=cmd.model, description=cmd.description)

        mutate_config(_m)
        return PersonaUpsertResult(
            persona=PersonaItem(name=cmd.name, soul=cmd.soul, model=cmd.model, description=cmd.description, builtin=False)
        )

    @route(
        "DELETE",
        "/api/v1/personas",
        scope=Scope.CONFIG_WRITE.value,
        request_model=PersonaDeleteCommand,
        response_model=PersonaDeleteResult,
        summary="Delete a user persona (built-ins cannot be deleted)",
    )
    async def delete_persona(self, cmd: PersonaDeleteCommand, principal: Principal) -> PersonaDeleteResult:
        principal.require(Scope.CONFIG_WRITE)
        cfg = load_config(get_config_path())
        if cmd.name not in cfg.personas:
            if cmd.name in BUILTIN_PERSONAS:
                raise ForbiddenError("built-in persona cannot be deleted", details={"name": cmd.name})
            raise NotFoundError("no such persona", details={"name": cmd.name})

        def _m(c: object) -> None:
            c.personas.pop(cmd.name, None)
            if c.agents.defaults.persona == cmd.name:
                c.agents.defaults.persona = None

        mutate_config(_m)
        return PersonaDeleteResult(ok=True)

    @route(
        "POST",
        "/api/v1/personas/default",
        scope=Scope.CONFIG_WRITE.value,
        request_model=SetDefaultPersonaCommand,
        response_model=SetDefaultPersonaResult,
        summary="Set (or clear) the global default persona",
    )
    async def set_default(self, cmd: SetDefaultPersonaCommand, principal: Principal) -> SetDefaultPersonaResult:
        principal.require(Scope.CONFIG_WRITE)
        if cmd.name is not None:
            cfg = load_config(get_config_path())
            if cmd.name not in cfg.persona_names():
                raise ValidationFailedError(f"unknown persona {cmd.name!r}")

        def _m(c: object) -> None:
            c.agents.defaults.persona = cmd.name

        mutate_config(_m)
        return SetDefaultPersonaResult(default=cmd.name)
