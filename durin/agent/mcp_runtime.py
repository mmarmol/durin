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

    async def connect(self, name: str) -> None:
        await self._loop.connect_mcp_server(name)

    async def disconnect(self, name: str) -> None:
        await self._loop.disconnect_mcp_server(name)
