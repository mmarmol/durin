"""McpService — manage MCP servers and their tools.

Wraps the configured ``tools.mcp_servers`` (CRUD via load/save_config), overlays
OAuth-credential presence (the secret store) and live connection state (an
optional :class:`~durin.agent.mcp_runtime.McpRuntime`), and toggles a server on
or off at runtime. Mirrors opencode's first-class MCP model: a per-server status
plus a single enable/disable that also connects/disconnects.

The runtime is optional: without it (TUI / contract generation) the service
reports config-only status and skips the live connect/disconnect side effects.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from durin.config.schema import MCPServerConfig
from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    ConflictError,
    NotFoundError,
    Query,
    Result,
    ValidationFailedError,
)

if TYPE_CHECKING:
    from durin.agent.mcp_runtime import McpRuntime, RawConnState


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class McpToolInfo(Result):
    name: str
    description: str


class McpServerSummary(Result):
    name: str
    transport: str  # "stdio" | "sse" | "streamableHttp" (declared or inferred)
    target: str  # the command (stdio) or url (http) — for at-a-glance display
    enabled: bool
    oauth_required: bool
    oauth_authenticated: bool
    status: str  # connected | connecting | failed | needs_auth | disabled
    tool_count: int
    error: str | None = None


class McpListResult(Result):
    servers: list[McpServerSummary]


class McpServerDetail(Result):
    name: str
    transport: str
    target: str
    enabled: bool
    oauth_required: bool
    oauth_authenticated: bool
    status: str
    error: str | None
    tools: list[McpToolInfo]
    config: MCPServerConfig


class McpListQuery(Query):
    """No inputs — lists all configured servers."""


class McpServerGetQuery(Query):
    name: str


class McpServerUpsertCommand(Command):
    """Create or replace a server. ``config`` is the full server config so the
    webui form can edit every field (basic + advanced)."""

    name: str
    config: MCPServerConfig


class McpServerNameCommand(Command):
    name: str


class McpOkResult(Result):
    ok: bool


class McpOauthLoginResult(Result):
    authorization_url: str
    state: str


# ---------------------------------------------------------------------------
# Status derivation (pure)
# ---------------------------------------------------------------------------


def derive_status(
    *,
    enabled: bool,
    oauth_required: bool,
    oauth_authenticated: bool,
    raw: "RawConnState | None",
) -> tuple[str, str | None]:
    """Map config + OAuth-credential + live-connection facts to a status.

    Returns ``(status, error)``. Precedence: ``disabled`` (config off) >
    ``needs_auth`` (OAuth server with no token) > the live connection state
    (``connected`` / ``failed`` / ``connecting``). ``raw is None`` means no live
    connection — the server is coming up (or the runtime is absent), reported as
    ``connecting``.
    """
    if not enabled:
        return ("disabled", None)
    if oauth_required and not oauth_authenticated:
        return ("needs_auth", None)
    if raw is None:
        return ("connecting", None)
    if raw.breaker_state == "closed":
        return ("connected", None)
    if raw.breaker_state == "open":
        return ("failed", raw.error or "connection unavailable")
    return ("connecting", None)  # half-open: a probe is in flight


def _transport(sc: MCPServerConfig) -> str:
    """The declared transport, or the obvious inference for display."""
    if sc.type:
        return sc.type
    return "stdio" if sc.command else "streamableHttp"


def _target(sc: MCPServerConfig) -> str:
    """A human-readable endpoint: the command line (stdio) or the URL (http)."""
    if sc.command:
        return " ".join([sc.command, *sc.args]).strip()
    return sc.url


def _validate_upsert(name: str, sc: MCPServerConfig) -> None:
    """Reject obviously-unusable server definitions (pydantic validates the rest)."""
    if not name or not name.strip():
        raise ValidationFailedError("server name is required", details={"field": "name"})
    if not sc.command and not sc.url:
        raise ValidationFailedError(
            "a server needs a command (stdio) or a url (http)",
            details={"field": "config"},
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class McpService:
    """Manage configured MCP servers, their tools, and OAuth credentials."""

    def __init__(self, *, mcp_runtime: "McpRuntime | None" = None) -> None:
        self._runtime = mcp_runtime

    def _live(self) -> dict[str, "RawConnState"]:
        return self._runtime.live_status() if self._runtime is not None else {}

    async def _oauth_flags(self, name: str, sc: MCPServerConfig) -> tuple[bool, bool]:
        """Return (oauth_required, oauth_authenticated) for a server."""
        if sc.oauth_config() is None:
            return (False, False)
        from durin.agent.tools.mcp_oauth import SecretsTokenStorage

        storage = SecretsTokenStorage(name, server_url=sc.url or None)
        return (True, (await storage.get_tokens()) is not None)

    async def _summary(
        self, name: str, sc: MCPServerConfig, raw: "RawConnState | None"
    ) -> McpServerSummary:
        required, authed = await self._oauth_flags(name, sc)
        status, error = derive_status(
            enabled=sc.enabled,
            oauth_required=required,
            oauth_authenticated=authed,
            raw=raw,
        )
        return McpServerSummary(
            name=name,
            transport=_transport(sc),
            target=_target(sc),
            enabled=sc.enabled,
            oauth_required=required,
            oauth_authenticated=authed,
            status=status,
            tool_count=len(raw.tools) if raw else 0,
            error=error,
        )

    @route(
        "GET",
        "/api/v1/mcp/servers",
        scope=Scope.MCP_READ.value,
        request_model=McpListQuery,
        response_model=McpListResult,
        summary="List MCP servers with live status",
    )
    async def list(self, query: McpListQuery, principal: Principal) -> McpListResult:
        principal.require(Scope.MCP_READ)
        from durin.config.loader import load_config

        cfg = load_config()
        live = self._live()
        servers = [
            await self._summary(name, sc, live.get(name))
            for name, sc in sorted(cfg.tools.mcp_servers.items())
        ]
        return McpListResult(servers=servers)

    @route(
        "GET",
        "/api/v1/mcp/servers/{name}",
        scope=Scope.MCP_READ.value,
        request_model=McpServerGetQuery,
        response_model=McpServerDetail,
        summary="Fetch an MCP server's config, status, and tools",
    )
    async def get(
        self, query: McpServerGetQuery, principal: Principal
    ) -> McpServerDetail:
        principal.require(Scope.MCP_READ)
        from durin.config.loader import load_config

        sc = load_config().tools.mcp_servers.get(query.name)
        if sc is None:
            raise NotFoundError("no such MCP server", details={"name": query.name})
        return await self._build_detail(query.name, sc)

    async def _build_detail(self, name: str, sc: MCPServerConfig) -> McpServerDetail:
        raw = self._live().get(name)
        required, authed = await self._oauth_flags(name, sc)
        status, error = derive_status(
            enabled=sc.enabled,
            oauth_required=required,
            oauth_authenticated=authed,
            raw=raw,
        )
        tools = [
            McpToolInfo(name=tname, description=desc)
            for (tname, desc) in (raw.tools if raw else [])
        ]
        return McpServerDetail(
            name=name,
            transport=_transport(sc),
            target=_target(sc),
            enabled=sc.enabled,
            oauth_required=required,
            oauth_authenticated=authed,
            status=status,
            error=error,
            tools=tools,
            config=sc,
        )

    @route(
        "POST",
        "/api/v1/mcp/servers",
        scope=Scope.MCP_WRITE.value,
        request_model=McpServerUpsertCommand,
        response_model=McpServerDetail,
        summary="Add an MCP server",
    )
    async def add(
        self, cmd: McpServerUpsertCommand, principal: Principal
    ) -> McpServerDetail:
        principal.require(Scope.MCP_WRITE)
        _validate_upsert(cmd.name, cmd.config)
        from durin.config.loader import get_config_path, load_config, save_config

        cfg = load_config()
        if cmd.name in cfg.tools.mcp_servers:
            raise ConflictError("MCP server already exists", details={"name": cmd.name})
        cfg.tools.mcp_servers[cmd.name] = cmd.config
        save_config(cfg, get_config_path())
        if cmd.config.enabled and self._runtime is not None:
            await self._runtime.connect(cmd.name, cmd.config)
        return await self._build_detail(cmd.name, cmd.config)

    @route(
        "PATCH",
        "/api/v1/mcp/servers/{name}",
        scope=Scope.MCP_WRITE.value,
        request_model=McpServerUpsertCommand,
        response_model=McpServerDetail,
        summary="Replace an MCP server's config",
    )
    async def update(
        self, cmd: McpServerUpsertCommand, principal: Principal
    ) -> McpServerDetail:
        principal.require(Scope.MCP_WRITE)
        _validate_upsert(cmd.name, cmd.config)
        from durin.config.loader import get_config_path, load_config, save_config

        cfg = load_config()
        if cmd.name not in cfg.tools.mcp_servers:
            raise NotFoundError("no such MCP server", details={"name": cmd.name})
        cfg.tools.mcp_servers[cmd.name] = cmd.config
        save_config(cfg, get_config_path())
        # Persist-only: a live connection keeps running with its current config
        # until the next enable/disable toggle re-applies it.
        return await self._build_detail(cmd.name, cmd.config)

    @route(
        "DELETE",
        "/api/v1/mcp/servers/{name}",
        scope=Scope.MCP_WRITE.value,
        request_model=McpServerNameCommand,
        response_model=McpOkResult,
        summary="Remove an MCP server",
    )
    async def remove(
        self, cmd: McpServerNameCommand, principal: Principal
    ) -> McpOkResult:
        principal.require(Scope.MCP_WRITE)
        from durin.config.loader import get_config_path, load_config, save_config

        cfg = load_config()
        if cmd.name not in cfg.tools.mcp_servers:
            raise NotFoundError("no such MCP server", details={"name": cmd.name})
        del cfg.tools.mcp_servers[cmd.name]
        save_config(cfg, get_config_path())
        if self._runtime is not None and cmd.name in self._live():
            await self._runtime.disconnect(cmd.name)
        return McpOkResult(ok=True)

    def _set_enabled(self, name: str, enabled: bool) -> MCPServerConfig:
        """Persist a server's enabled flag; return the (mutated) config."""
        from durin.config.loader import get_config_path, load_config, save_config

        cfg = load_config()
        sc = cfg.tools.mcp_servers.get(name)
        if sc is None:
            raise NotFoundError("no such MCP server", details={"name": name})
        sc.enabled = enabled
        save_config(cfg, get_config_path())
        return sc

    @route(
        "POST",
        "/api/v1/mcp/servers/{name}/enable",
        scope=Scope.MCP_WRITE.value,
        request_model=McpServerNameCommand,
        response_model=McpServerDetail,
        summary="Enable a server and connect it",
    )
    async def enable(
        self, cmd: McpServerNameCommand, principal: Principal
    ) -> McpServerDetail:
        principal.require(Scope.MCP_WRITE)
        sc = self._set_enabled(cmd.name, True)
        if self._runtime is not None:
            await self._runtime.connect(cmd.name, sc)
        return await self._build_detail(cmd.name, sc)

    @route(
        "POST",
        "/api/v1/mcp/servers/{name}/disable",
        scope=Scope.MCP_WRITE.value,
        request_model=McpServerNameCommand,
        response_model=McpServerDetail,
        summary="Disable a server and disconnect it",
    )
    async def disable(
        self, cmd: McpServerNameCommand, principal: Principal
    ) -> McpServerDetail:
        principal.require(Scope.MCP_WRITE)
        sc = self._set_enabled(cmd.name, False)
        if self._runtime is not None and cmd.name in self._live():
            await self._runtime.disconnect(cmd.name)
        return await self._build_detail(cmd.name, sc)

    @route(
        "POST",
        "/api/v1/mcp/servers/{name}/oauth/logout",
        scope=Scope.MCP_WRITE.value,
        request_model=McpServerNameCommand,
        response_model=McpOkResult,
        summary="Clear stored OAuth tokens for a server",
    )
    async def oauth_logout(
        self, cmd: McpServerNameCommand, principal: Principal
    ) -> McpOkResult:
        principal.require(Scope.MCP_WRITE)
        from durin.config.loader import load_config

        sc = load_config().tools.mcp_servers.get(cmd.name)
        if sc is None:
            raise NotFoundError("no such MCP server", details={"name": cmd.name})
        from durin.agent.tools.mcp_oauth import SecretsTokenStorage

        SecretsTokenStorage(cmd.name, server_url=sc.url or None).forget()
        return McpOkResult(ok=True)
