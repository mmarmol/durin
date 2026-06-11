"""MCP tool deferral behind a discovery bridge (P3, 2026-06-10).

When the aggregate schema size of registered MCP tools crosses the
configured threshold, their definitions stop shipping to the LLM
(``Tool.llm_visible`` → False; the registry excludes them from
``get_definitions``) and two bridge tools take their place:

- ``mcp_find_tools(query)`` — search the deferred catalog, get full
  schemas back. Its description embeds a one-line-per-tool catalog so
  the model knows what exists without paying for the schemas.
- ``mcp_invoke(name, arguments)`` — execute a deferred tool by name.

Built-in tools are NEVER deferred — durin's curated capability surface
(memory_search included) must stay structurally visible; deferring it
would recreate the silent-miss problem at the tool layer. MCP is the
unbounded third-party surface where definition bloat actually comes
from. Below the threshold, everything registers exactly as before —
one small server doesn't pay the discovery indirection.

Deferral is decided once per process, after all servers connect
(``AgentLoop._connect_mcp``). Tools are matched by the ``mcp_`` name
prefix — the same convention ``ToolRegistry.get_definitions`` uses for
its sort order.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.registry import ToolRegistry
from durin.agent.tools.schema import (
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from durin.utils.helpers import estimate_text_tokens

__all__ = ["maybe_defer_mcp_tools"]

_BRIDGE_NAMES = frozenset({"mcp_find_tools", "mcp_invoke"})
_FIND_MAX_SCHEMAS = 8
_CATALOG_LINE_DESC_CHARS = 100


def _deferrable_mcp_tools(registry: ToolRegistry) -> list[Tool]:
    tools = []
    for name in registry.tool_names:
        if not name.startswith("mcp_") or name in _BRIDGE_NAMES:
            continue
        tool = registry.get(name)
        if tool is not None:
            tools.append(tool)
    return tools


def _catalog_line(tool: Tool) -> str:
    desc = " ".join((tool.description or "").split())
    if len(desc) > _CATALOG_LINE_DESC_CHARS:
        desc = desc[:_CATALOG_LINE_DESC_CHARS] + "…"
    return f"- {tool.name}: {desc}" if desc else f"- {tool.name}"


def maybe_defer_mcp_tools(registry: ToolRegistry, config: Any) -> int:
    """Defer MCP tool definitions when they exceed the threshold.

    ``config`` is the ``tools.mcp_deferral`` section (or None). Returns
    the number of deferred tools; 0 means nothing changed.
    """
    if config is None or not getattr(config, "enabled", False):
        return 0
    threshold = int(getattr(config, "threshold_tokens", 0) or 0)
    if threshold <= 0:
        return 0
    candidates = _deferrable_mcp_tools(registry)
    if not candidates:
        return 0
    estimate = estimate_text_tokens(
        json.dumps([t.to_schema() for t in candidates], default=str)
    )
    if estimate <= threshold:
        return 0

    for tool in candidates:
        tool._llm_visible = False  # noqa: SLF001 - the flag Tool.llm_visible reads
    registry.register(McpFindToolsTool(registry))
    registry.register(McpInvokeTool(registry))
    logger.info(
        "MCP deferral active: {} tool definitions (~{} tokens > {} threshold) "
        "behind mcp_find_tools/mcp_invoke",
        len(candidates), estimate, threshold,
    )
    return len(candidates)


_FIND_PARAMETERS = tool_parameters_schema(
    query=StringSchema(
        "Substring matched against deferred tool names and descriptions "
        "(case-insensitive). Empty or omitted lists the full catalog "
        "(names + descriptions, no schemas)."
    ),
    required=[],
)


@tool_parameters(_FIND_PARAMETERS)
class McpFindToolsTool(Tool):
    """Discovery bridge over the deferred MCP catalog."""

    _plugin_discoverable = False

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "mcp_find_tools"

    @property
    def read_only(self) -> bool:
        return True

    def _deferred(self) -> list[Tool]:
        return [
            t for t in _deferrable_mcp_tools(self._registry)
            if not t.llm_visible
        ]

    @property
    def description(self) -> str:
        catalog = "\n".join(_catalog_line(t) for t in self._deferred())
        return (
            "Many MCP tools are available but their schemas are not "
            "loaded to save context. Search here to get full schemas, "
            "then call a tool with mcp_invoke(name, arguments).\n\n"
            "Deferred catalog:\n" + (catalog or "(empty)")
        )

    async def execute(self, **kwargs: Any) -> Any:
        query = str(kwargs.get("query") or "").strip().casefold()
        deferred = self._deferred()
        if not query:
            return {
                "tools": [_catalog_line(t) for t in deferred],
                "hint": "Pass a query to get full parameter schemas.",
            }
        matches = [
            t for t in deferred
            if query in t.name.casefold()
            or query in (t.description or "").casefold()
        ]
        schemas = [t.to_schema() for t in matches[:_FIND_MAX_SCHEMAS]]
        result: dict[str, Any] = {"matches": schemas, "total": len(matches)}
        if len(matches) > _FIND_MAX_SCHEMAS:
            result["hint"] = (
                f"{len(matches) - _FIND_MAX_SCHEMAS} more matches not shown; "
                "narrow the query."
            )
        return result


_INVOKE_PARAMETERS = tool_parameters_schema(
    name=StringSchema(
        "Exact deferred tool name as returned by mcp_find_tools "
        "(e.g. 'mcp_github_create_issue')."
    ),
    arguments=ObjectSchema(
        description=(
            "Arguments object for the tool, matching the schema from "
            "mcp_find_tools. Empty object if the tool takes no arguments."
        ),
        additional_properties=True,
    ),
    required=["name"],
)


@tool_parameters(_INVOKE_PARAMETERS)
class McpInvokeTool(Tool):
    """Execution bridge for deferred MCP tools."""

    _plugin_discoverable = False

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "mcp_invoke"

    @property
    def description(self) -> str:
        return (
            "Execute a deferred MCP tool by name. Use mcp_find_tools "
            "first to discover the tool's parameter schema; argument "
            "validation happens at execution time."
        )

    async def execute(self, **kwargs: Any) -> Any:
        name = str(kwargs.get("name") or "").strip()
        arguments = kwargs.get("arguments")
        if isinstance(arguments, str) and arguments.strip():
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return {"error": "arguments must be a JSON object"}
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return {"error": "arguments must be a JSON object"}

        tool = self._registry.get(name)
        if (
            tool is None
            or not name.startswith("mcp_")
            or name in _BRIDGE_NAMES
            or tool.llm_visible
        ):
            return {
                "error": (
                    f"'{name}' is not a deferred MCP tool. "
                    "Use mcp_find_tools to list what is available."
                ),
            }
        return await tool.execute(**arguments)
