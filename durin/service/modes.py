"""ModesService — list and manage the registered agent modes.

``list`` projects every registered mode (built-ins plus user-defined) for the
composer picker and the settings editor. ``upsert`` / ``delete`` persist custom
modes in config (``agent_modes``) and re-register them so the change takes effect
without a restart; the built-ins (build/plan/explore) are immutable.

``tools`` returns the catalog of agent tools a mode allowlist can reference, so
the editor offers a checklist of the REAL tool set instead of free-text typing.
It reads the live loop's tool registry (the faithful source — built-ins plus any
connected MCP tools), falling back to loader discovery when no live loop is wired.

Escape hatch: ``ModesResult.modes`` / ``ToolsResult.tools`` are ``list[dict[str,
Any]]`` — each entry is a small open dict.
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


def _project_tool(name: str, tool: Any) -> dict[str, Any]:
    """Project a registered Tool to the open dict the mode editor consumes.

    ``read_only`` powers the editor's "these read-only tools are not in this
    mode" hint (surfacing drift the user must resolve by hand); ``source`` lets
    the UI group the built-in surface separately from dynamic MCP tools.
    ``background`` says whether an allowlist entry for this tool can ever apply
    to a sub-agent or workflow work node: built-ins must carry the ``subagent``
    scope; MCP tools reach nodes through the node's ``mcps`` field instead of a
    scope, so they always count.
    """
    is_mcp = name.startswith("mcp_")
    scopes = sorted(getattr(type(tool), "_scopes", {"core"}))
    return {
        "name": name,
        "description": (getattr(tool, "description", "") or "").strip(),
        "read_only": bool(getattr(tool, "read_only", False)),
        "source": "mcp" if is_mcp else "builtin",
        "background": True if is_mcp else "subagent" in scopes,
    }


def _fallback_tool_registry() -> Any | None:
    """Best-effort tool registry from loader discovery under the current config.

    Used only when no live loop is wired (OpenAPI spec generation, the ws-channel
    shim registry). Returns the core built-ins; config-gated or live-loop-wired
    tools may be absent — the live-registry path (production) is complete.
    """
    try:
        from durin.agent.tools.context import ToolContext
        from durin.agent.tools.loader import ToolLoader
        from durin.agent.tools.registry import ToolRegistry

        cfg = load_config(get_config_path())
        ctx = ToolContext(
            config=cfg.tools,
            workspace=str(cfg.workspace_path),
            app_config=cfg,
        )
        registry = ToolRegistry()
        ToolLoader().load(ctx, registry, scope="core")
        return registry
    except Exception:
        return None


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


class ToolsListQuery(Query):
    """No inputs — lists every agent tool a mode allowlist can reference."""


class ToolsResult(Result):
    tools: list[dict[str, Any]]  # escape hatch — open per-tool dict


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ModesService:
    """List and manage agent modes (built-ins are read-only)."""

    def __init__(self, tool_registry_resolver: Any = None) -> None:
        # Returns the live loop's ToolRegistry so the /api/v1/tools catalog
        # reflects exactly what the running agent can call. None → loader
        # discovery fallback (spec generation / deps-less registries).
        self._tool_registry_resolver = tool_registry_resolver

    def _resolve_tool_registry(self) -> Any | None:
        if self._tool_registry_resolver is not None:
            try:
                reg = self._tool_registry_resolver()
                if reg is not None:
                    return reg
            except Exception:
                pass
        return _fallback_tool_registry()

    @route(
        "GET",
        "/api/v1/tools",
        scope=Scope.SYSTEM_READ.value,
        request_model=ToolsListQuery,
        response_model=ToolsResult,
        summary="List agent tools available to reference in a mode allowlist",
    )
    async def tools(self, query: ToolsListQuery, principal: Principal) -> ToolsResult:
        principal.require(Scope.SYSTEM_READ)
        registry = self._resolve_tool_registry()
        catalog: list[dict[str, Any]] = []
        if registry is not None:
            for name in registry.tool_names:
                tool = registry.get(name)
                if tool is not None:
                    catalog.append(_project_tool(name, tool))
        # Built-ins first (the primary curation surface), then MCP; A→Z within.
        catalog.sort(key=lambda t: (t["source"] != "builtin", t["name"]))
        return ToolsResult(tools=catalog)

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
