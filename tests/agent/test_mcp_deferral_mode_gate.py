"""A deferred MCP tool answers to the active agent mode like a direct call.

Once MCP deferral is active, the model reaches MCP tools through the
``mcp_find_tools`` / ``mcp_invoke`` bridges. The runner's mode gate sees
only the bridge name, so the bridges themselves must apply the same
decision to the inner tool: a tool the mode refuses is neither listed nor
run, and a tool it allows runs with the same argument preparation a
direct call gets.

The MCP tools here are real ``MCPToolWrapper`` instances over a fake
server connection, registered and deferred the way the gateway does it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.agent.agent_mode import BUILD_MODE, AgentMode
from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.agent.tools.mcp import MCPToolWrapper
from durin.agent.tools.mcp_deferral import maybe_defer_mcp_tools
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse, ToolCallRequest

mcp_types = pytest.importorskip("mcp.types")

_MAX_CHARS = AgentDefaults().max_tool_result_chars

CREATE = "mcp_tracker_create_ticket"
LIST = "mcp_tracker_list_tickets"


class _FakeConnection:
    """Stands in for ``MCPServerConnection``: the wrapper only calls
    ``call_tool(name, arguments, timeout=...)`` and reads the result."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float):
        self.calls.append((name, dict(arguments)))
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=f"{name} done")],
        )


def _registry() -> tuple[ToolRegistry, _FakeConnection]:
    conn = _FakeConnection()
    registry = ToolRegistry()
    registry.register(MCPToolWrapper(conn, "tracker", mcp_types.Tool(
        name="create_ticket",
        description="Create a ticket in the tracker",
        input_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )))
    registry.register(MCPToolWrapper(conn, "tracker", mcp_types.Tool(
        name="list_tickets",
        description="List tickets in the tracker",
        input_schema={"type": "object", "properties": {}},
    )))
    deferred = maybe_defer_mcp_tools(
        registry, MagicMock(enabled=True, threshold_tokens=1),
    )
    assert deferred == 2
    return registry, conn


DENY_CREATE = AgentMode(
    name="no-create", description="denies one MCP tool",
    denied=frozenset({CREATE}),
)
ALLOWLIST_WITHOUT_CREATE = AgentMode(
    name="tracker-read", description="allowlist without the create tool",
    allowed=frozenset({"mcp_find_tools", "mcp_invoke", LIST}),
)
ALLOWLIST_WITH_CREATE = AgentMode(
    name="tracker-write", description="allowlist with the create tool",
    allowed=frozenset({"mcp_find_tools", "mcp_invoke", LIST, CREATE}),
)
REFUSE_INVOKE = AgentMode(
    name="no-invoke", description="denies the invoke bridge",
    denied=frozenset({"mcp_invoke"}),
)

_UNSET = object()


def _spec(registry: ToolRegistry, mode: Any = _UNSET) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[],
        tools=registry,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_CHARS,
        mode_provider=None if mode is _UNSET else (lambda: mode),
    )


async def _call(
    registry: ToolRegistry, mode: Any, name: str, arguments: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    runner = AgentRunner(MagicMock())
    results, events, _ = await runner._execute_tools(
        _spec(registry, mode),
        [ToolCallRequest(id="c1", name=name, arguments=arguments)],
        {}, {}, set(),
    )
    return results[0], events[0]


def _invoke(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [DENY_CREATE, ALLOWLIST_WITHOUT_CREATE])
async def test_refused_tool_is_refused_through_mcp_invoke_like_a_direct_call(mode):
    registry, conn = _registry()

    direct, direct_event = await _call(registry, mode, CREATE, {"count": 1})
    bridged, _ = await _call(registry, mode, "mcp_invoke", _invoke(CREATE, {"count": 1}))

    assert conn.calls == [], "a refused tool must never reach the MCP server"
    assert direct_event["detail"] == f"denied by mode '{mode.name}'"
    assert bridged == direct


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [ALLOWLIST_WITH_CREATE, BUILD_MODE, _UNSET],
    ids=["allowlist", "build", "no-mode"],
)
async def test_allowed_tool_runs_through_mcp_invoke_with_cast_arguments(mode):
    registry, conn = _registry()

    direct, _ = await _call(registry, mode, CREATE, {"count": "3"})
    bridged, event = await _call(
        registry, mode, "mcp_invoke", _invoke(CREATE, {"count": "3"}),
    )

    assert event["status"] == "ok"
    assert bridged == direct
    # Both calls reached the server with the string cast to the schema's integer.
    assert conn.calls == [("create_ticket", {"count": 3}), ("create_ticket", {"count": 3})]


@pytest.mark.asyncio
async def test_invalid_argument_through_mcp_invoke_gives_the_direct_call_error():
    registry, conn = _registry()

    direct, _ = await _call(registry, ALLOWLIST_WITH_CREATE, CREATE, {"count": "many"})
    bridged, event = await _call(
        registry, ALLOWLIST_WITH_CREATE, "mcp_invoke", _invoke(CREATE, {"count": "many"}),
    )

    assert conn.calls == []
    assert event["status"] == "error"
    assert bridged == direct


@pytest.mark.asyncio
async def test_mode_refusing_mcp_invoke_still_refuses_it():
    registry, conn = _registry()

    result, event = await _call(registry, REFUSE_INVOKE, "mcp_invoke", _invoke(LIST, {}))

    assert conn.calls == []
    assert event["detail"] == "denied by mode 'no-invoke'"
    assert "mcp_invoke" in result


def _found_names(result: dict[str, Any]) -> set[str]:
    if "matches" in result:
        return {m["function"]["name"] for m in result["matches"]}
    return {line[2:].split(":", 1)[0] for line in result["tools"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["tracker", ""], ids=["search", "catalog"])
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (DENY_CREATE, {LIST}),
        (ALLOWLIST_WITHOUT_CREATE, {LIST}),
        (ALLOWLIST_WITH_CREATE, {LIST, CREATE}),
        (BUILD_MODE, {LIST, CREATE}),
        (_UNSET, {LIST, CREATE}),
    ],
    ids=["deny", "allowlist-without", "allowlist-with", "build", "no-mode"],
)
async def test_mcp_find_tools_results_follow_the_mode(mode, expected, query):
    registry, _ = _registry()

    result, event = await _call(registry, mode, "mcp_find_tools", {"query": query})

    assert event["status"] == "ok"
    assert _found_names(result) == expected


def _find_tools_description(registry: ToolRegistry, mode: Any) -> str:
    for schema in AgentRunner._active_tool_definitions(_spec(registry, mode)):
        if schema["function"]["name"] == "mcp_find_tools":
            return schema["function"]["description"]
    raise AssertionError("mcp_find_tools not offered")


@pytest.mark.parametrize("restricted_first", [True, False])
def test_mcp_find_tools_catalog_follows_the_mode_on_a_shared_registry(restricted_first):
    """One registry serves every session, and it caches tool schemas. The
    catalog a restricted turn sees must not leak into an unrestricted one
    (or the other way round), whichever renders first."""
    registry, _ = _registry()
    order = [DENY_CREATE, BUILD_MODE] if restricted_first else [BUILD_MODE, DENY_CREATE]

    descriptions = {mode.name: _find_tools_description(registry, mode) for mode in order}

    assert CREATE not in descriptions[DENY_CREATE.name]
    assert LIST in descriptions[DENY_CREATE.name]
    assert CREATE in descriptions[BUILD_MODE.name]
    assert LIST in descriptions[BUILD_MODE.name]


@pytest.mark.asyncio
async def test_full_run_refuses_a_denied_tool_through_mcp_invoke():
    """End to end through ``AgentRunner.run``: the model asks for the denied
    tool through the bridge and gets the refusal; the server is never hit."""
    registry, conn = _registry()
    provider = MagicMock()
    seen: list[list[dict[str, Any]]] = []

    async def chat_with_retry(*, messages, **kwargs):
        seen.append(list(messages))
        if len(seen) == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="c1", name="mcp_invoke", arguments=_invoke(CREATE, {"count": 1}),
                )],
                usage={},
            )
        return LLMResponse(content="could not create it", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "create a ticket"}],
        tools=registry,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_CHARS,
        mode_provider=lambda: DENY_CREATE,
    ))

    assert result.final_content == "could not create it"
    assert conn.calls == []
    tool_messages = [m for m in seen[1] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert f"Tool '{CREATE}' is not available in mode 'no-create'" in str(
        tool_messages[0]["content"]
    )
