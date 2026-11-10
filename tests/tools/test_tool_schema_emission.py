"""What a tool definition sent to a model contains.

A tool's description travels in the function's own ``description`` field. The
parameters object beside it must not carry the same text again at its root:
that would send every such description twice in each request. Everything else
in the parameters object — property descriptions, constraints, a root note that
says something of its own — is emitted as the tool authored it.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import mcp.types as mcp_types
import pytest

from durin.agent.tools.base import Tool
from durin.agent.tools.context import AuxProviderHandle, ToolContext
from durin.agent.tools.loader import ToolLoader
from durin.agent.tools.mcp import MCPToolWrapper
from durin.agent.tools.mcp_deferral import maybe_defer_mcp_tools
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import ToolsConfig
from durin.providers.anthropic_provider import AnthropicProvider


def _rich_ctx(workspace: Path, *, scope: str = "core") -> ToolContext:
    """A context with every service present, so no built-in is gated off."""
    return ToolContext(
        config=ToolsConfig(),
        workspace=str(workspace),
        bus=MagicMock(),
        subagent_manager=MagicMock(),
        cron_service=MagicMock(),
        automations_runtime=MagicMock(),
        sessions=MagicMock(),
        aux_providers={
            "vision": AuxProviderHandle(provider=MagicMock(), model="vis-model"),
            "audio": AuxProviderHandle(provider=MagicMock(), model="aud-model"),
        },
        scope=scope,
    )


def _every_builtin(workspace: Path) -> list[Tool]:
    """One instance of every tool class the loader discovers."""
    ctx = _rich_ctx(workspace)
    tools = [cls.create(ctx) for cls in ToolLoader().discover()]
    assert tools, "the loader discovered no built-in tools"
    return tools


def _occurrences(node: Any, text: str) -> int:
    """How many string values in a JSON-like tree are exactly ``text``."""
    if isinstance(node, str):
        return int(node == text)
    if isinstance(node, dict):
        return sum(_occurrences(v, text) for v in node.values())
    if isinstance(node, list):
        return sum(_occurrences(v, text) for v in node)
    return 0


def test_every_builtin_emits_its_description_once(tmp_path):
    counts = {
        tool.name: _occurrences(tool.to_schema(), tool.description)
        for tool in _every_builtin(tmp_path)
    }
    assert {name: n for name, n in counts.items() if n != 1} == {}


@pytest.mark.parametrize("scope", ["core", "subagent"])
def test_loaded_registry_definitions_carry_each_description_once(tmp_path, scope):
    """The definitions list a main loop, a subagent and a workflow node send."""
    registry = ToolRegistry()
    loaded = ToolLoader().load(_rich_ctx(tmp_path, scope=scope), registry, scope=scope)
    assert loaded

    definitions = registry.get_definitions()
    assert len(definitions) == len(loaded)
    repeated = [
        d["function"]["name"]
        for d in definitions
        if _occurrences(d, d["function"]["description"]) != 1
    ]
    assert repeated == []


def test_anthropic_input_schema_does_not_repeat_the_description(tmp_path):
    registry = ToolRegistry()
    for tool in _every_builtin(tmp_path):
        registry.register(tool)

    converted = AnthropicProvider._convert_tools(registry.get_definitions())

    assert len(converted) == len(registry.get_definitions())
    repeated = [
        entry["name"]
        for entry in converted
        if _occurrences(entry["input_schema"], entry["description"]) != 0
    ]
    assert repeated == []


def test_emitted_parameters_differ_from_authored_only_by_the_repeated_root(tmp_path):
    """Property descriptions, constraints and required lists are untouched."""
    for tool in _every_builtin(tmp_path):
        authored = tool.parameters
        emitted = tool.to_schema()["function"]["parameters"]

        assert emitted.get("properties") == authored.get("properties"), tool.name
        dropped = {k: v for k, v in authored.items() if k not in emitted}
        assert dropped in ({}, {"description": tool.description}), tool.name
        assert {k: authored[k] for k in emitted} == emitted, tool.name


def test_root_note_that_is_not_the_tool_description_is_emitted(tmp_path):
    """``cron`` describes its per-action requirements at the schema root, in
    words that are not its tool description; the model must still get them."""
    registry = ToolRegistry()
    ToolLoader().load(_rich_ctx(tmp_path), registry)
    cron = registry.get("cron")
    assert cron is not None

    root_note = cron.parameters["description"]
    assert root_note != cron.description
    assert cron.to_schema()["function"]["parameters"]["description"] == root_note


def test_emitting_a_schema_leaves_the_tool_as_it_was(tmp_path):
    """Tools that read their description from the schema root keep finding it
    there, and a second emission equals the first."""
    for tool in _every_builtin(tmp_path):
        authored = deepcopy(tool.parameters)
        description = tool.description

        first = deepcopy(tool.to_schema())

        assert tool.parameters == authored, tool.name
        assert tool.description == description, tool.name
        assert tool.to_schema() == first, tool.name


def test_validation_and_casting_of_a_tool_with_a_root_description(tmp_path):
    registry = ToolRegistry()
    for tool in _every_builtin(tmp_path):
        registry.register(tool)
    search = registry.get("memory_search")
    assert search is not None
    assert search.parameters["description"] == search.description
    registry.get_definitions()

    _tool, cast, error = registry.prepare_call(
        "memory_search", {"query": "tea", "limit": "5", "kinds": "skill"},
    )
    assert error is None
    assert cast == {"query": "tea", "limit": 5, "kinds": "skill"}

    _tool, cast, error = registry.prepare_call(
        "memory_search", {"limit": 500, "kinds": "everything"},
    )
    assert cast == {"limit": 500, "kinds": "everything"}
    assert error == (
        "Error: Invalid parameters for tool 'memory_search': "
        "missing required query; "
        "limit must be <= 50; "
        "kinds must be one of ['all', 'skill', 'fact']"
    )


def _mcp_wrapper(*, description: str, input_schema: dict[str, Any]) -> MCPToolWrapper:
    tool_def = mcp_types.Tool(
        name="lookup_order", description=description, input_schema=input_schema,
    )
    return MCPToolWrapper(SimpleNamespace(), "orders", tool_def)


_ORDER_SCHEMA = {
    "type": "object",
    "description": "Arguments of an order lookup.",
    "properties": {
        "order_id": {"type": "string", "description": "The order's identifier."},
    },
    "required": ["order_id"],
}


def test_mcp_tool_keeps_its_servers_root_description():
    wrapper = _mcp_wrapper(
        description="Look up one order by its identifier.",
        input_schema=deepcopy(_ORDER_SCHEMA),
    )

    function = wrapper.to_schema()["function"]

    assert function["description"] == "Look up one order by its identifier."
    assert function["parameters"] == _ORDER_SCHEMA


def test_deferred_mcp_tool_listing_keeps_the_servers_root_description():
    registry = ToolRegistry()
    registry.register(_mcp_wrapper(
        description="Look up one order by its identifier.",
        input_schema=deepcopy(_ORDER_SCHEMA),
    ))
    deferred = maybe_defer_mcp_tools(
        registry, SimpleNamespace(enabled=True, threshold_tokens=1),
    )
    assert deferred == 1

    found = asyncio.run(registry.get("mcp_find_tools").execute(query="lookup_order"))

    assert [m["function"]["parameters"] for m in found["matches"]] == [_ORDER_SCHEMA]
