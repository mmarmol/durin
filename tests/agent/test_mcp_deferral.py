"""Tests for P3 — MCP tool deferral behind the discovery bridge.

Above the threshold, MCP tool definitions stop shipping to the LLM and
mcp_find_tools / mcp_invoke take their place. Built-ins are never
deferred; below the threshold nothing changes. No `mcp` package needed
— the deferral keys off the `mcp_` name-prefix convention, so stub
Tools suffice.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.mcp_deferral import maybe_defer_mcp_tools
from durin.agent.tools.registry import ToolRegistry
from durin.agent.tools.schema import StringSchema, tool_parameters_schema


@tool_parameters(tool_parameters_schema(
    arg=StringSchema("an argument"), required=[],
))
class _StubMcpTool(Tool):
    _plugin_discoverable = False

    def __init__(self, name: str, description: str = "does a thing") -> None:
        self._name = name
        self._description = description
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return f"{self._name} ran"


@tool_parameters(tool_parameters_schema(required=[]))
class _BuiltinTool(Tool):
    _plugin_discoverable = False

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "built-in"

    async def execute(self, **kwargs: Any) -> Any:
        return "ok"


def _registry(mcp_count: int = 3) -> tuple[ToolRegistry, list[_StubMcpTool]]:
    registry = ToolRegistry()
    registry.register(_BuiltinTool())
    stubs = [
        _StubMcpTool(f"mcp_srv_tool{i}", f"tool number {i} for testing")
        for i in range(mcp_count)
    ]
    for s in stubs:
        registry.register(s)
    return registry, stubs


def _cfg(enabled: bool = True, threshold_tokens: int = 1) -> SimpleNamespace:
    return SimpleNamespace(enabled=enabled, threshold_tokens=threshold_tokens)


def _definition_names(registry: ToolRegistry) -> set[str]:
    out = set()
    for schema in registry.get_definitions():
        out.add(schema["function"]["name"])
    return out


def test_below_threshold_changes_nothing():
    registry, _ = _registry()
    deferred = maybe_defer_mcp_tools(registry, _cfg(threshold_tokens=10_000_000))
    assert deferred == 0
    names = _definition_names(registry)
    assert "mcp_srv_tool0" in names
    assert "mcp_find_tools" not in names


def test_disabled_config_changes_nothing():
    registry, _ = _registry()
    assert maybe_defer_mcp_tools(registry, _cfg(enabled=False)) == 0
    assert maybe_defer_mcp_tools(registry, None) == 0


def test_above_threshold_defers_mcp_but_never_builtins():
    registry, stubs = _registry()
    deferred = maybe_defer_mcp_tools(registry, _cfg(threshold_tokens=1))

    assert deferred == len(stubs)
    names = _definition_names(registry)
    assert "read_file" in names                  # built-ins untouched
    assert "mcp_find_tools" in names             # bridges visible
    assert "mcp_invoke" in names
    assert "mcp_srv_tool0" not in names          # deferred hidden
    # ...but still registered and executable.
    assert registry.has("mcp_srv_tool0")


def test_find_tools_lists_catalog_and_returns_schemas():
    registry, _ = _registry()
    maybe_defer_mcp_tools(registry, _cfg(threshold_tokens=1))
    find = registry.get("mcp_find_tools")

    assert "mcp_srv_tool1" in find.description   # catalog in description

    listing = asyncio.run(find.execute())
    assert any("mcp_srv_tool2" in line for line in listing["tools"])

    result = asyncio.run(find.execute(query="tool1"))
    assert result["total"] == 1
    assert result["matches"][0]["function"]["name"] == "mcp_srv_tool1"
    assert "parameters" in result["matches"][0]["function"]


def test_invoke_executes_deferred_tool_with_arguments():
    registry, stubs = _registry()
    maybe_defer_mcp_tools(registry, _cfg(threshold_tokens=1))
    invoke = registry.get("mcp_invoke")

    result = asyncio.run(
        invoke.execute(name="mcp_srv_tool0", arguments={"arg": "x"})
    )

    assert result == "mcp_srv_tool0 ran"
    assert stubs[0].calls == [{"arg": "x"}]


def test_invoke_accepts_json_string_arguments():
    registry, stubs = _registry()
    maybe_defer_mcp_tools(registry, _cfg(threshold_tokens=1))
    invoke = registry.get("mcp_invoke")

    result = asyncio.run(
        invoke.execute(name="mcp_srv_tool1", arguments='{"arg": "y"}')
    )

    assert result == "mcp_srv_tool1 ran"
    assert stubs[1].calls == [{"arg": "y"}]


def test_invoke_rejects_builtins_and_unknown_and_visible_tools():
    registry, _ = _registry()
    maybe_defer_mcp_tools(registry, _cfg(threshold_tokens=1))
    invoke = registry.get("mcp_invoke")

    assert "error" in asyncio.run(invoke.execute(name="read_file"))
    assert "error" in asyncio.run(invoke.execute(name="mcp_nope"))
    assert "error" in asyncio.run(invoke.execute(name="mcp_invoke"))


def test_invoke_without_deferral_rejects_visible_mcp_tool():
    registry, _ = _registry()
    from durin.agent.tools.mcp_deferral import McpInvokeTool
    invoke = McpInvokeTool(registry)

    result = asyncio.run(invoke.execute(name="mcp_srv_tool0"))
    assert "error" in result
