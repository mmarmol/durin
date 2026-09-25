"""Read-only view over the gateway's live MCP connections.

The durin gateway runs a single long-lived ``AgentLoop`` whose
``_mcp_connections`` hold one supervised ``MCPServerConnection`` per connected
server. ``McpRuntime`` is the thin handle the gateway passes to the MCP service
so it can report live per-server status and drive runtime connect/disconnect —
without the service reaching into ``AgentLoop`` internals directly.

It is intentionally optional: the TUI and the OpenAPI contract generator build
the service registry with no runtime, in which case the service falls back to
config-only status.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RawConnState:
    """A live MCP connection's observable state, as read from the loop.

    ``breaker_state`` is the circuit-breaker value ("closed" | "open" |
    "half-open"); ``tools`` is the list of (wrapped tool name, description)
    the connection has registered.
    """

    breaker_state: str
    error: str | None
    tools: list[tuple[str, str]]


class McpRuntime:
    """A handle over the gateway ``AgentLoop`` for live MCP status + control."""

    def __init__(self, loop: Any) -> None:
        self._loop = loop

    def live_status(self) -> dict[str, RawConnState]:
        """Snapshot the state of every currently-live MCP connection."""
        registry = getattr(self._loop, "tools", None)
        out: dict[str, RawConnState] = {}
        for name, conn in self._loop._mcp_connections.items():
            tools: list[tuple[str, str]] = []
            for tool_name in getattr(conn, "_registered_names", []):
                tool = registry.get(tool_name) if registry is not None else None
                desc = getattr(tool, "description", "") if tool is not None else ""
                tools.append((tool_name, desc))
            err = getattr(conn, "_error", None)
            out[name] = RawConnState(
                breaker_state=conn.breaker_state().value,
                error=str(err) if err is not None else None,
                tools=tools,
            )
        return out

    def connect_errors(self) -> dict[str, str]:
        """Last connect-failure message per server.

        Populated when an enabled server fails to connect (so the service can
        report ``failed`` instead of a perpetual ``connecting``); cleared on a
        successful connect or an intentional disconnect.
        """
        return dict(getattr(self._loop, "_mcp_connect_errors", {}))

    async def connect(self, name: str, cfg: Any = None) -> None:
        await self._loop.connect_mcp_server(name, cfg)

    async def disconnect(self, name: str) -> None:
        await self._loop.disconnect_mcp_server(name)

    def mark_approved(self, name: str, cfg: Any) -> None:
        """Record *cfg* as the config a person or an approved request just
        put in place for *name*, without connecting.

        ``connect_mcp_server`` already does this as a side effect of an
        actual connect (below); this covers ``McpService`` callers that
        persist without connecting — ``update`` is deliberately persist-only,
        and ``add(..., connect=False)`` backgrounds the connect. Both are
        still an authorized change (a person's REST/dashboard action, or the
        approval executor applying an already-approved ``mcp_manage``
        request) and must be on record as such.
        """
        self._loop._mcp_servers[name] = cfg

    def approved_config(self, name: str) -> Any | None:
        """The config a person or an approved request most recently put in
        place for *name*, or ``None`` when this process holds no record for
        it at all (e.g. it appeared in ``config.json`` through some path
        outside durin's own write surface — a hand-edit, or a write while the
        gateway was down).

        Backed by ``AgentLoop._mcp_servers``, which starts as the boot-time
        config snapshot (rule: whatever a person already had on disk when
        durin started is trusted) and is kept current by ``mark_approved``
        (``McpService.add``/``update``/``enable``/``registry_update``) and by
        ``connect_mcp_server`` itself on every actual connect — including a
        person's own ``reconnect`` from the dashboard, which legitimizes
        whatever is on disk at that moment going forward.

        Used by ``mcp_manage``'s agent-facing ``reconnect`` action ONLY: the
        shared ``McpService.reconnect`` (dashboard/REST) always trusts the
        caller and is not gated by this at all. The agent's own bare
        reconnect has no such standing — it refuses instead of picking up a
        config nobody here ever approved.
        """
        return getattr(self._loop, "_mcp_servers", {}).get(name)
