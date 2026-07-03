"""PersonasService — SOUL personality files + persona config CRUD.

Two surfaces:
- SOUL files: list, upsert, delete named SOUL.md personality files in the workspace.
- Personas: list, upsert, delete named PersonaConfig entries in config; set or clear
  the global default persona. Example personas are seeded into config on first run
  (durin.personas.seed_example_personas) as ordinary editable/deletable entries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from durin.config.loader import get_config_path, load_config, mutate_config
from durin.config.schema import PersonaConfig
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
# Persona test DTOs
# ---------------------------------------------------------------------------

_TEST_PROMPT = "Reply with a brief one-sentence greeting in your own voice."


class PersonaTestCommand(Command):
    model: str | None = None
    soul: str | None = None


class PersonaTestResult(Result):
    ok: bool
    reply: str | None = None
    error: str | None = None
    model: str | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PersonasService:
    """Read and mutate SOUL personality files in the workspace."""

    def __init__(
        self,
        workspace_resolver: Callable[[], Path],
        on_config_changed: Callable[[], None] | None = None,
    ) -> None:
        self._workspace = workspace_resolver
        self._on_config_changed = on_config_changed

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
        # Any soul except `default` can be deleted, even when a persona references
        # it: a dangling reference falls back to the default SOUL at runtime (see
        # AgentLoop._active_persona), so deletion never breaks the agent.
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
        summary="List personas and the default",
    )
    async def list_personas(self, query: PersonaListQuery, principal: Principal) -> PersonaListResult:
        principal.require(Scope.CONFIG_READ)
        cfg = load_config(get_config_path())
        items: list[PersonaItem] = []
        for name, p in cfg.personas.items():
            items.append(PersonaItem(name=name, soul=p.soul, model=p.model, description=p.description, builtin=False))
        items.sort(key=lambda i: i.name)
        # The implicit base — the default SOUL + the default model — listed LAST so it is
        # visible and selectable like any other persona. It is the fallback used when no
        # persona is active; it cannot be edited or deleted as a persona (edit the default
        # SOUL via the SOUL library, the default model via agent settings). Named "durin"
        # since it IS durin's base voice, not an anonymous default.
        if "durin" not in cfg.personas:
            items.append(
                PersonaItem(
                    name="durin",
                    soul="default",
                    model=None,
                    description="durin's base voice — the default SOUL with the default model.",
                    builtin=False,
                )
            )
        # When no persona is configured, the synthetic "durin" is the active default. A
        # legacy config with the old "default" name stored still displays as "durin".
        configured = cfg.agents.defaults.persona
        default = "durin" if configured in (None, "default") else configured
        return PersonaListResult(personas=items, default=default)

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
        # "durin"/"default"/"none" are reserved for the synthetic base entry — a user
        # persona by those names would hijack the immutable default slot in the listing.
        if cmd.name in ("durin", "default", "none"):
            raise ValidationFailedError(f"{cmd.name!r} is a reserved persona name")

        def _m(cfg: object) -> None:
            cfg.personas[cmd.name] = PersonaConfig(soul=cmd.soul, model=cmd.model, description=cmd.description)

        mutate_config(_m)
        if self._on_config_changed is not None:
            self._on_config_changed()
        return PersonaUpsertResult(
            persona=PersonaItem(name=cmd.name, soul=cmd.soul, model=cmd.model, description=cmd.description, builtin=False)
        )

    @route(
        "DELETE",
        "/api/v1/personas",
        scope=Scope.CONFIG_WRITE.value,
        request_model=PersonaDeleteCommand,
        response_model=PersonaDeleteResult,
        summary="Delete a persona",
    )
    async def delete_persona(self, cmd: PersonaDeleteCommand, principal: Principal) -> PersonaDeleteResult:
        principal.require(Scope.CONFIG_WRITE)
        cfg = load_config(get_config_path())
        if cmd.name not in cfg.personas:
            raise NotFoundError("no such persona", details={"name": cmd.name})

        def _m(c: object) -> None:
            c.personas.pop(cmd.name, None)
            if c.agents.defaults.persona == cmd.name:
                c.agents.defaults.persona = None

        mutate_config(_m)
        if self._on_config_changed is not None:
            self._on_config_changed()
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
        # Selecting the synthetic "durin" (or the legacy "default"/"none" aliases) clears
        # the override → the base.
        name = None if cmd.name in (None, "durin", "default", "none") else cmd.name
        if name is not None:
            cfg = load_config(get_config_path())
            if name not in cfg.persona_names():
                raise ValidationFailedError(f"unknown persona {name!r}")

        def _m(c: object) -> None:
            c.agents.defaults.persona = name

        mutate_config(_m)
        if self._on_config_changed is not None:
            self._on_config_changed()
        return SetDefaultPersonaResult(default=name)

    @route(
        "POST",
        "/api/v1/personas/test",
        scope=Scope.CONFIG_READ.value,
        request_model=PersonaTestCommand,
        response_model=PersonaTestResult,
        summary="Live round-trip: run the chosen SOUL + model with a short prompt",
    )
    async def test_persona(self, cmd: PersonaTestCommand, principal: Principal) -> PersonaTestResult:
        principal.require(Scope.CONFIG_READ)
        from durin.command.builtin import adhoc_preset_config
        from durin.providers.factory import make_provider

        cfg = load_config(get_config_path())
        ref = (cmd.model or "").strip()
        try:
            parts = ref.split()
            if len(parts) == 2:
                preset = adhoc_preset_config(cfg, parts[0], parts[1])
            else:
                preset = cfg.resolve_preset(ref or None)
        except Exception as e:  # noqa: BLE001
            return PersonaTestResult(ok=False, error=f"Could not resolve model {ref or 'default'!r}: {e}")

        system = ""
        if cmd.soul:
            try:
                system = self._store().read(cmd.soul) or ""
            except ValueError:
                system = ""
        messages = (
            [{"role": "system", "content": system}] if system else []
        ) + [{"role": "user", "content": _TEST_PROMPT}]

        try:
            provider = make_provider(cfg, preset=preset)
            resp = await provider.chat_with_retry(
                messages=messages,
                tools=None,
                model=preset.model,
                max_tokens=256,
                temperature=0.2,
                retry_mode="standard",
            )
        except Exception as e:  # noqa: BLE001
            return PersonaTestResult(ok=False, error=f"{type(e).__name__}: {e}", model=preset.model)

        if getattr(resp, "finish_reason", None) == "error":
            return PersonaTestResult(ok=False, error=(getattr(resp, "content", None) or "Provider error."), model=preset.model)
        content = getattr(resp, "content", None)
        if not content:
            return PersonaTestResult(ok=False, error="Model returned an empty response.", model=preset.model)
        return PersonaTestResult(ok=True, reply=content[:2000], model=preset.model)
