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
from durin.service.types import Command, Query, Result

if TYPE_CHECKING:
    from durin.agent.mcp_runtime import RawConnState


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
