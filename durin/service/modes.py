"""ModesService — list and manage the registered agent modes.

``list`` projects every registered mode (built-ins plus user-defined) for the
composer picker and the settings editor. ``upsert`` / ``delete`` persist custom
modes in config (``agent_modes``) and re-register them so the change takes effect
without a restart; the built-ins (build/plan/explore) are immutable.

Escape hatch: ``ModesResult.modes`` is ``list[dict[str, Any]]`` — each mode is a
small open dict (name/description/icon/builtin plus access detail).
"""

from __future__ import annotations

from typing import Any

from durin.config.loader import get_config_path, load_config, mutate_config
from durin.config.schema import ModeConfig
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


def _project(mode: Any) -> dict[str, Any]:
    """Project an AgentMode to the open dict the UI consumes (picker + editor)."""
    return {
        "name": mode.name,
        "description": mode.description,
        "icon": mode.icon,
        "builtin": mode.builtin,
        "allowed": sorted(mode.allowed) if mode.allowed is not None else None,
        "denied": sorted(mode.denied),
        "prompt_suffix": mode.prompt_suffix,
    }


def _builtin_names() -> set[str]:
    from durin.agent.agent_mode import list_modes

    return {m.name for m in list_modes() if m.builtin}


def _reregister() -> None:
    """Reload config modes into the registry so a mutation takes effect live."""
    from durin.agent.agent_mode import register_config_modes

    register_config_modes(load_config(get_config_path()).agent_modes)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class ModesListQuery(Query):
    """No inputs — lists every registered agent mode."""


class ModesResult(Result):
    modes: list[dict[str, Any]]  # escape hatch — open per-mode dict


class ModeUpsertCommand(Command):
    name: str
    description: str = ""
    allowed: list[str] | None = None  # None = full access (subject to denied)
    denied: list[str] = []
    prompt_suffix: str = ""
    icon: str | None = None


class ModeUpsertResult(Result):
    mode: dict[str, Any]


class ModeDeleteCommand(Command):
    name: str


class ModeDeleteResult(Result):
    ok: bool


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ModesService:
    """List and manage agent modes (built-ins are read-only)."""

    @route(
        "GET",
        "/api/v1/modes",
        scope=Scope.SYSTEM_READ.value,
        request_model=ModesListQuery,
        response_model=ModesResult,
        summary="List registered agent modes (build/plan/explore plus custom)",
    )
    async def list(self, query: ModesListQuery, principal: Principal) -> ModesResult:
        principal.require(Scope.SYSTEM_READ)
        from durin.agent.agent_mode import list_modes

        return ModesResult(modes=[_project(m) for m in list_modes()])

    @route(
        "POST",
        "/api/v1/modes",
        scope=Scope.CONFIG_WRITE.value,
        request_model=ModeUpsertCommand,
        response_model=ModeUpsertResult,
        summary="Create or update a custom agent mode",
    )
    async def upsert(self, cmd: ModeUpsertCommand, principal: Principal) -> ModeUpsertResult:
        principal.require(Scope.CONFIG_WRITE)
        from durin.agent.agent_mode import get_mode

        name = cmd.name.strip()
        if not name or any(c.isspace() for c in name):
            raise ValidationFailedError("mode name must be non-empty and contain no spaces")
        if name in _builtin_names():
            raise ForbiddenError(f"{name!r} is a built-in mode and cannot be edited")

        def _m(cfg: object) -> None:
            cfg.agent_modes[name] = ModeConfig(
                description=cmd.description,
                allowed=cmd.allowed,
                denied=list(cmd.denied),
                prompt_suffix=cmd.prompt_suffix,
                icon=cmd.icon,
            )

        mutate_config(_m)
        _reregister()
        return ModeUpsertResult(mode=_project(get_mode(name)))

    @route(
        "DELETE",
        "/api/v1/modes",
        scope=Scope.CONFIG_WRITE.value,
        request_model=ModeDeleteCommand,
        response_model=ModeDeleteResult,
        summary="Delete a custom agent mode",
    )
    async def delete(self, cmd: ModeDeleteCommand, principal: Principal) -> ModeDeleteResult:
        principal.require(Scope.CONFIG_WRITE)

        name = cmd.name.strip()
        if name in _builtin_names():
            raise ForbiddenError(f"{name!r} is a built-in mode and cannot be deleted")
        if name not in load_config(get_config_path()).agent_modes:
            raise NotFoundError("no such mode", details={"name": name})

        def _m(c: object) -> None:
            c.agent_modes.pop(name, None)

        mutate_config(_m)
        _reregister()
        return ModeDeleteResult(ok=True)
