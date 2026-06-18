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

from typing import TYPE_CHECKING, Any

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


# --- Registry discovery DTOs (Phase 2) ---------------------------------------


class McpRegistrySearchQuery(Query):
    q: str = ""
    limit: int = 10


class McpRegistryDescribeQuery(Query):
    ref: str


class McpRegistryInstallCommand(Command):
    ref: str
    prefer: str = "remote"  # "remote" | "local"
    env_values: dict[str, str] | None = None  # required/secret env collected from the user


class McpUpdatesQuery(Query):
    """No inputs — checks every configured server against the registry."""


class McpUpdateInfo(Result):
    name: str
    current: str
    latest: str


class McpUpdatesResult(Result):
    updates: list[McpUpdateInfo]


class McpRegistryHit(Result):
    name: str
    ref: str
    registry: str
    kind: str  # remote | local | both
    description: str
    signals: dict


class McpRegistrySearchResult(Result):
    hits: list[McpRegistryHit]


class McpRegistryEnvVar(Result):
    name: str
    description: str
    is_required: bool
    is_secret: bool
    default: str | None


class McpRegistryPackage(Result):
    registry_type: str
    identifier: str
    version: str
    runtime_hint: str
    transport_type: str
    runtime_arguments: list[str]
    package_arguments: list[str]
    env: list[McpRegistryEnvVar]


class McpRegistryRemote(Result):
    transport_type: str
    url: str
    headers: list[McpRegistryEnvVar]


class McpRegistryServerDetail(Result):
    name: str
    ref: str
    description: str
    version: str
    repository: str
    packages: list[McpRegistryPackage]
    remotes: list[McpRegistryRemote]


def _reg_envvar(e: Any) -> McpRegistryEnvVar:
    return McpRegistryEnvVar(
        name=e.name, description=e.description, is_required=e.is_required,
        is_secret=e.is_secret, default=e.default)


def _reg_hit(h: Any) -> McpRegistryHit:
    return McpRegistryHit(
        name=h.name, ref=h.ref, registry=h.registry, kind=h.kind,
        description=h.description, signals=h.signals)


def _reg_detail(d: Any) -> McpRegistryServerDetail:
    return McpRegistryServerDetail(
        name=d.name, ref=d.ref, description=d.description, version=d.version,
        repository=d.repository,
        packages=[
            McpRegistryPackage(
                registry_type=p.registry_type, identifier=p.identifier,
                version=p.version, runtime_hint=p.runtime_hint,
                transport_type=p.transport_type,
                runtime_arguments=p.runtime_arguments,
                package_arguments=p.package_arguments,
                env=[_reg_envvar(e) for e in p.env])
            for p in d.packages
        ],
        remotes=[
            McpRegistryRemote(
                transport_type=r.transport_type, url=r.url,
                headers=[_reg_envvar(e) for e in r.headers])
            for r in d.remotes
        ])


# ---------------------------------------------------------------------------
# Status derivation (pure)
# ---------------------------------------------------------------------------


def derive_status(
    *,
    enabled: bool,
    oauth_required: bool,
    oauth_authenticated: bool,
    raw: "RawConnState | None",
    connect_error: str | None = None,
) -> tuple[str, str | None]:
    """Map config + OAuth-credential + live-connection facts to a status.

    Returns ``(status, error)``. Precedence: ``disabled`` (config off) >
    ``needs_auth`` (OAuth server with no token) > the live connection state
    (``connected`` / ``failed`` / ``connecting``). When there is no live
    connection (``raw is None``): a recorded ``connect_error`` (the last connect
    attempt failed) surfaces as ``failed`` — matching opencode, so a broken
    server isn't shown as a perpetual ``connecting`` — otherwise the server is
    coming up (or the runtime is absent) and is reported as ``connecting``.
    """
    if not enabled:
        return ("disabled", None)
    if oauth_required and not oauth_authenticated:
        return ("needs_auth", None)
    if raw is None:
        if connect_error:
            return ("failed", connect_error)
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

    def __init__(
        self,
        *,
        mcp_runtime: "McpRuntime | None" = None,
        oauth_flows: Any = None,
    ) -> None:
        self._runtime = mcp_runtime
        self._oauth_flows = oauth_flows

    def _live(self) -> dict[str, "RawConnState"]:
        return self._runtime.live_status() if self._runtime is not None else {}

    def _connect_errors(self) -> dict[str, str]:
        if self._runtime is None:
            return {}
        getter = getattr(self._runtime, "connect_errors", None)
        return getter() if callable(getter) else {}

    def _flows(self) -> Any:
        """The OAuth flow orchestrator (lazily constructed; injectable in tests)."""
        if self._oauth_flows is None:
            from durin.agent.tools.mcp_oauth_web import McpOauthFlows

            self._oauth_flows = McpOauthFlows()
        return self._oauth_flows

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
            connect_error=self._connect_errors().get(name),
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
        "/api/v1/mcp/registry/search",
        scope=Scope.MCP_READ.value,
        request_model=McpRegistrySearchQuery,
        response_model=McpRegistrySearchResult,
        summary="Search the MCP registry for installable servers",
    )
    async def registry_search(
        self, query: McpRegistrySearchQuery, principal: Principal
    ) -> McpRegistrySearchResult:
        principal.require(Scope.MCP_READ)
        from durin.agent.mcp_catalog_cache import McpCatalogCache
        from durin.agent.mcp_registry import build_mcp_adapters, search_mcp_registries
        from durin.config.loader import get_config_path, load_config

        disc = load_config().tools.mcp_discovery
        cache = McpCatalogCache(get_config_path().parent / "mcp_catalog.json")
        hits = await search_mcp_registries(
            query.q,
            cache=cache,
            adapters=build_mcp_adapters(disc.registries),
            limit=query.limit or disc.search_limit,
        )
        return McpRegistrySearchResult(hits=[_reg_hit(h) for h in hits])

    @route(
        "GET",
        "/api/v1/mcp/registry/describe",
        scope=Scope.MCP_READ.value,
        request_model=McpRegistryDescribeQuery,
        response_model=McpRegistryServerDetail,
        summary="Full install metadata for one registry server",
    )
    async def registry_describe(
        self, query: McpRegistryDescribeQuery, principal: Principal
    ) -> McpRegistryServerDetail:
        principal.require(Scope.MCP_READ)
        from durin.agent.mcp_registry import build_mcp_adapters
        from durin.config.loader import load_config

        for adapter in build_mcp_adapters(load_config().tools.mcp_discovery.registries):
            detail = await adapter.describe(query.ref)
            if detail is not None:
                return _reg_detail(detail)
        raise NotFoundError("server not found in registry", details={"ref": query.ref})

    @route(
        "POST",
        "/api/v1/mcp/registry/install",
        scope=Scope.MCP_WRITE.value,
        request_model=McpRegistryInstallCommand,
        response_model=McpServerDetail,
        summary="Install (add) an MCP server from the registry by ref",
    )
    async def registry_install(
        self, cmd: McpRegistryInstallCommand, principal: Principal
    ) -> McpServerDetail:
        principal.require(Scope.MCP_WRITE)
        from durin.agent.mcp_install import (
            build_server_config_from_detail,
            collect_secret_env,
        )
        from durin.agent.mcp_registry import build_mcp_adapters
        from durin.config.loader import load_config

        disc = load_config().tools.mcp_discovery
        detail = None
        for adapter in build_mcp_adapters(disc.registries):
            detail = await adapter.describe(cmd.ref)
            if detail is not None:
                break
        if detail is None:
            raise NotFoundError("server not found in registry", details={"ref": cmd.ref})
        server_name = cmd.ref.rsplit("/", 1)[-1] or cmd.ref
        secret_refs = collect_secret_env(
            detail, cmd.env_values or {}, server_name=server_name
        )
        sc = build_server_config_from_detail(
            detail, prefer=cmd.prefer, secret_env_refs=secret_refs
        )
        return await self.add(
            McpServerUpsertCommand(name=server_name, config=sc), principal
        )

    @route(
        "GET",
        "/api/v1/mcp/registry/updates",
        scope=Scope.MCP_READ.value,
        request_model=McpUpdatesQuery,
        response_model=McpUpdatesResult,
        summary="List configured servers with a newer version in the registry",
    )
    async def registry_updates(
        self, query: McpUpdatesQuery, principal: Principal
    ) -> McpUpdatesResult:
        principal.require(Scope.MCP_READ)
        import asyncio

        from durin.agent.mcp_install import has_update
        from durin.agent.mcp_registry import build_mcp_adapters
        from durin.config.loader import load_config

        cfg = load_config()
        adapters = build_mcp_adapters(cfg.tools.mcp_discovery.registries)
        candidates = [
            (name, sc)
            for name, sc in sorted(cfg.tools.mcp_servers.items())
            if sc.source_ref and sc.version
        ]

        async def _latest(ref: str):
            for adapter in adapters:
                detail = await adapter.describe(ref)
                if detail is not None:
                    return detail
            return None

        # Describe every configured server concurrently rather than one-at-a-time.
        details = await asyncio.gather(*[_latest(sc.source_ref) for _, sc in candidates])
        out = [
            McpUpdateInfo(name=name, current=sc.version, latest=detail.version)
            for (name, sc), detail in zip(candidates, details)
            if detail is not None and has_update(sc.version, detail.version)
        ]
        return McpUpdatesResult(updates=out)

    @route(
        "POST",
        "/api/v1/mcp/servers/{name}/registry-update",
        scope=Scope.MCP_WRITE.value,
        request_model=McpServerNameCommand,
        response_model=McpServerDetail,
        summary="Re-pin a server to the registry's latest version and reconnect",
    )
    async def registry_update(
        self, cmd: McpServerNameCommand, principal: Principal
    ) -> McpServerDetail:
        principal.require(Scope.MCP_WRITE)
        from durin.agent.mcp_install import rebuild_for_update
        from durin.agent.mcp_registry import build_mcp_adapters
        from durin.config.loader import load_config

        cfg = load_config()
        sc = cfg.tools.mcp_servers.get(cmd.name)
        if sc is None:
            raise NotFoundError("no such MCP server", details={"name": cmd.name})
        if not sc.source_ref:
            raise ValidationFailedError(
                "server was not installed from the registry",
                details={"name": cmd.name},
            )
        detail = None
        for adapter in build_mcp_adapters(cfg.tools.mcp_discovery.registries):
            detail = await adapter.describe(sc.source_ref)
            if detail is not None:
                break
        if detail is None:
            raise NotFoundError(
                "server not found in registry", details={"ref": sc.source_ref}
            )
        await self.update(
            McpServerUpsertCommand(name=cmd.name, config=rebuild_for_update(sc, detail)),
            principal,
        )
        return await self.reconnect(McpServerNameCommand(name=cmd.name), principal)

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
            connect_error=self._connect_errors().get(name),
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
        "/api/v1/mcp/servers/{name}/reconnect",
        scope=Scope.MCP_WRITE.value,
        request_model=McpServerNameCommand,
        response_model=McpServerDetail,
        summary="Reconnect a server to apply config changes or retry a failure",
    )
    async def reconnect(
        self, cmd: McpServerNameCommand, principal: Principal
    ) -> McpServerDetail:
        principal.require(Scope.MCP_WRITE)
        from durin.config.loader import load_config

        sc = load_config().tools.mcp_servers.get(cmd.name)
        if sc is None:
            raise NotFoundError("no such MCP server", details={"name": cmd.name})
        # Apply the current config to the live connection (and retry failures).
        # A disabled server has nothing to (re)connect.
        if self._runtime is not None and sc.enabled:
            await self._runtime.disconnect(cmd.name)
            await self._runtime.connect(cmd.name, sc)
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

    @route(
        "POST",
        "/api/v1/mcp/servers/{name}/oauth/login",
        scope=Scope.MCP_WRITE.value,
        request_model=McpServerNameCommand,
        response_model=McpOauthLoginResult,
        summary="Start interactive OAuth sign-in; returns the authorization URL",
    )
    async def oauth_login(
        self, cmd: McpServerNameCommand, principal: Principal
    ) -> McpOauthLoginResult:
        principal.require(Scope.MCP_WRITE)
        from durin.config.loader import load_config

        sc = load_config().tools.mcp_servers.get(cmd.name)
        if sc is None:
            raise NotFoundError("no such MCP server", details={"name": cmd.name})
        if sc.oauth_config() is None:
            raise ValidationFailedError(
                "server is not OAuth-enabled", details={"name": cmd.name}
            )

        runtime = self._runtime
        name = cmd.name

        async def _reconnect_with_token() -> None:
            # Runs after the token is stored: reconnect the live connection so it
            # builds a fresh provider that finds the token and lists tools.
            if runtime is not None:
                await runtime.disconnect(name)
                await runtime.connect(name, sc)

        url, state = await self._flows().start(name, sc, on_success=_reconnect_with_token)
        return McpOauthLoginResult(authorization_url=url, state=state)
