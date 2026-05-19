"""Tests for AgentRunner tool execution: batching, concurrency, exclusive tools."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.tools.base import Tool
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars

class _DelayTool(Tool):
    def __init__(
        self,
        name: str,
        *,
        delay: float,
        read_only: bool,
        shared_events: list[str],
        exclusive: bool = False,
    ):
        self._name = name
        self._delay = delay
        self._read_only = read_only
        self._shared_events = shared_events
        self._exclusive = exclusive

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def exclusive(self) -> bool:
        return self._exclusive

    async def execute(self, **kwargs):
        self._shared_events.append(f"start:{self._name}")
        await asyncio.sleep(self._delay)
        self._shared_events.append(f"end:{self._name}")
        return self._name


@pytest.mark.asyncio
async def test_runner_batches_read_only_tools_before_exclusive_work():
    from durin.agent.runner import AgentRunSpec, AgentRunner

    tools = ToolRegistry()
    shared_events: list[str] = []
    read_a = _DelayTool("read_a", delay=0.05, read_only=True, shared_events=shared_events)
    read_b = _DelayTool("read_b", delay=0.05, read_only=True, shared_events=shared_events)
    write_a = _DelayTool("write_a", delay=0.01, read_only=False, shared_events=shared_events)
    tools.register(read_a)
    tools.register(read_b)
    tools.register(write_a)

    runner = AgentRunner(MagicMock())
    await runner._execute_tools(
        AgentRunSpec(
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            concurrent_tools=True,
        ),
        [
            ToolCallRequest(id="ro1", name="read_a", arguments={}),
            ToolCallRequest(id="ro2", name="read_b", arguments={}),
            ToolCallRequest(id="rw1", name="write_a", arguments={}),
        ],
        {},
        {},
        set(),
    )

    assert shared_events[0:2] == ["start:read_a", "start:read_b"]
    assert "end:read_a" in shared_events and "end:read_b" in shared_events
    assert shared_events.index("end:read_a") < shared_events.index("start:write_a")
    assert shared_events.index("end:read_b") < shared_events.index("start:write_a")
    assert shared_events[-2:] == ["start:write_a", "end:write_a"]


@pytest.mark.asyncio
async def test_runner_does_not_batch_exclusive_read_only_tools():
    from durin.agent.runner import AgentRunSpec, AgentRunner

    tools = ToolRegistry()
    shared_events: list[str] = []
    read_a = _DelayTool("read_a", delay=0.03, read_only=True, shared_events=shared_events)
    read_b = _DelayTool("read_b", delay=0.03, read_only=True, shared_events=shared_events)
    ddg_like = _DelayTool(
        "ddg_like",
        delay=0.01,
        read_only=True,
        shared_events=shared_events,
        exclusive=True,
    )
    tools.register(read_a)
    tools.register(ddg_like)
    tools.register(read_b)

    runner = AgentRunner(MagicMock())
    await runner._execute_tools(
        AgentRunSpec(
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            concurrent_tools=True,
        ),
        [
            ToolCallRequest(id="ro1", name="read_a", arguments={}),
            ToolCallRequest(id="ddg1", name="ddg_like", arguments={}),
            ToolCallRequest(id="ro2", name="read_b", arguments={}),
        ],
        {},
        {},
        set(),
    )

    assert shared_events[0] == "start:read_a"
    assert shared_events.index("end:read_a") < shared_events.index("start:ddg_like")
    assert shared_events.index("end:ddg_like") < shared_events.index("start:read_b")


@pytest.mark.asyncio
async def test_runner_blocks_repeated_external_fetches():
    from durin.agent.runner import AgentRunSpec, AgentRunner

    provider = MagicMock()
    captured_final_call: list[dict] = []
    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 3:
            return LLMResponse(
                content="working",
                tool_calls=[ToolCallRequest(id=f"call_{call_count['n']}", name="web_fetch", arguments={"url": "https://example.com"})],
                usage={},
            )
        captured_final_call[:] = messages
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="page content")

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "research task"}],
        tools=tools,
        model="test-model",
        max_iterations=4,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert result.final_content == "done"
    assert tools.execute.await_count == 2
    blocked_tool_message = [
        msg for msg in captured_final_call
        if msg.get("role") == "tool" and msg.get("tool_call_id") == "call_3"
    ][0]
    assert "repeated external lookup blocked" in blocked_tool_message["content"]


# --- 1A: hash-based loop detection ---


class _FailingTool(Tool):
    """A tool that raises on every call, for loop-detection tests."""

    def __init__(self) -> None:
        self.call_count = 0

    @property
    def name(self) -> str:
        return "always_fails"

    @property
    def description(self) -> str:
        return "test"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"x": {"type": "integer"}}, "required": []}

    async def execute(self, **kwargs):
        self.call_count += 1
        raise RuntimeError("kaboom")


@pytest.mark.asyncio
async def test_loop_detection_blocks_repeated_failed_call():
    """A second identical call to a tool that already failed in this turn
    must short-circuit with the loop-block message instead of re-executing."""
    from durin.agent.runner import AgentRunSpec, AgentRunner, _LOOP_BLOCK_MESSAGE

    tools = ToolRegistry()
    failing = _FailingTool()
    tools.register(failing)
    runner = AgentRunner(MagicMock())
    seen: set[str] = set()

    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )

    # First call: actually executes, fails, signature recorded
    results1, _, _ = await runner._execute_tools(
        spec,
        [ToolCallRequest(id="c1", name="always_fails", arguments={"x": 1})],
        {}, {}, seen,
    )
    assert failing.call_count == 1
    assert "kaboom" in results1[0]
    assert len(seen) == 1

    # Second call with IDENTICAL args: must be blocked, NOT re-executed
    results2, _, _ = await runner._execute_tools(
        spec,
        [ToolCallRequest(id="c2", name="always_fails", arguments={"x": 1})],
        {}, {}, seen,
    )
    assert failing.call_count == 1, "tool should NOT have been executed a second time"
    assert results2[0] == _LOOP_BLOCK_MESSAGE


@pytest.mark.asyncio
async def test_loop_detection_allows_different_args():
    """The same tool with DIFFERENT arguments must NOT be blocked, even after
    a failure with a different arg set."""
    from durin.agent.runner import AgentRunSpec, AgentRunner

    tools = ToolRegistry()
    failing = _FailingTool()
    tools.register(failing)
    runner = AgentRunner(MagicMock())
    seen: set[str] = set()

    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )

    await runner._execute_tools(
        spec,
        [ToolCallRequest(id="c1", name="always_fails", arguments={"x": 1})],
        {}, {}, seen,
    )
    await runner._execute_tools(
        spec,
        [ToolCallRequest(id="c2", name="always_fails", arguments={"x": 2})],
        {}, {}, seen,
    )
    # Both calls should have executed (different args -> different signature)
    assert failing.call_count == 2
    assert len(seen) == 2


def test_tool_call_signature_normalizes_dict_order():
    """Signature must be stable across different dict key order — same args
    in different order must yield the same hash so we don't get false negatives."""
    from durin.agent.runner import _tool_call_signature

    sig1 = _tool_call_signature("edit_file", {"path": "a.py", "content": "x"})
    sig2 = _tool_call_signature("edit_file", {"content": "x", "path": "a.py"})
    assert sig1 == sig2

    # Different content -> different signature
    sig3 = _tool_call_signature("edit_file", {"path": "a.py", "content": "y"})
    assert sig1 != sig3

    # Different tool -> different signature
    sig4 = _tool_call_signature("write_file", {"path": "a.py", "content": "x"})
    assert sig1 != sig4


# --- 1B: topological ordering — verify mutation between reads breaks the batch ---


@pytest.mark.asyncio
async def test_runner_serializes_mutation_between_reads():
    """If the model emits [read, write, read], the write MUST complete before
    the second read starts. Reads on either side of the write are NOT batched
    together even though both are read-only, because reordering would break
    the read-after-write semantics the model expects.
    """
    from durin.agent.runner import AgentRunSpec, AgentRunner

    tools = ToolRegistry()
    shared_events: list[str] = []
    read_a = _DelayTool("read_a", delay=0.02, read_only=True, shared_events=shared_events)
    write_b = _DelayTool("write_b", delay=0.02, read_only=False, shared_events=shared_events)
    read_c = _DelayTool("read_c", delay=0.02, read_only=True, shared_events=shared_events)
    tools.register(read_a)
    tools.register(write_b)
    tools.register(read_c)

    runner = AgentRunner(MagicMock())
    await runner._execute_tools(
        AgentRunSpec(
            initial_messages=[],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
            concurrent_tools=True,
        ),
        [
            ToolCallRequest(id="r1", name="read_a", arguments={}),
            ToolCallRequest(id="w1", name="write_b", arguments={}),
            ToolCallRequest(id="r2", name="read_c", arguments={}),
        ],
        {}, {}, set(),
    )

    # read_a completes before write_b starts (own batch boundary)
    assert shared_events.index("end:read_a") < shared_events.index("start:write_b")
    # write_b completes before read_c starts (own batch boundary)
    assert shared_events.index("end:write_b") < shared_events.index("start:read_c")


# --- 2B: reasoning-phase truncation recovery ---


@pytest.mark.asyncio
async def test_reasoning_truncation_triggers_specialized_recovery():
    """When finish_reason=length hits with empty content AND non-empty
    reasoning_content, we must NOT route through the normal empty-retry path.
    Instead, append the partial reasoning + the reasoning-truncation cue,
    and continue with a fresh LLM call.
    """
    from durin.agent.runner import AgentRunSpec, AgentRunner
    from durin.utils.runtime import REASONING_TRUNCATION_PROMPT

    provider = MagicMock()
    captured_messages: list[list[dict]] = []

    async def chat_with_retry(*, messages, **kwargs):
        captured_messages.append(list(messages))
        if len(captured_messages) == 1:
            # First call: reasoning got truncated, content blank, reasoning huge
            return LLMResponse(
                content="",
                reasoning_content="(very long internal deliberation that ran out of tokens)",
                tool_calls=[],
                finish_reason="length",
                usage={},
            )
        # Second call (after recovery cue): final answer
        return LLMResponse(content="here is the answer", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "solve this hard problem"}],
        tools=tools,
        model="test-model",
        max_iterations=4,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    # We expect the run to succeed: 2 LLM calls (first truncated, second wraps up)
    assert result.final_content == "here is the answer"
    assert len(captured_messages) == 2

    # The second call should have the reasoning-truncation cue in its messages
    second_call_user_messages = [
        m for m in captured_messages[1]
        if m.get("role") == "user"
    ]
    assert any(REASONING_TRUNCATION_PROMPT in (m.get("content") or "") for m in second_call_user_messages), \
        "reasoning-truncation cue should appear in the second call's messages"


# --- Sprint B / L3: mode-based tool denial ---


class _BenignTool(Tool):
    """Tool that records calls; used to verify denial blocks execution."""

    def __init__(self, name: str = "edit_file") -> None:
        self._name = name
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "test"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        self.call_count += 1
        return "ok"


@pytest.mark.asyncio
async def test_runner_denies_tool_when_mode_disallows():
    """When the mode_provider returns a mode that disallows the tool, the
    runner short-circuits with a clear denial — the tool never executes."""
    from durin.agent.agent_mode import PLAN_MODE
    from durin.agent.runner import AgentRunSpec, AgentRunner

    tools = ToolRegistry()
    tool = _BenignTool(name="edit_file")  # edit_file is denied by PLAN_MODE
    tools.register(tool)
    runner = AgentRunner(MagicMock())
    seen: set[str] = set()

    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        mode_provider=lambda: PLAN_MODE,
    )

    results, events, _ = await runner._execute_tools(
        spec,
        [ToolCallRequest(id="c1", name="edit_file", arguments={})],
        {}, {}, seen,
    )
    assert tool.call_count == 0, "denied tool must NOT execute"
    assert "not available in mode 'plan'" in results[0]
    assert events[0]["status"] == "error"
    assert "denied by mode 'plan'" in events[0]["detail"]


@pytest.mark.asyncio
async def test_runner_no_denial_in_build_mode():
    """BUILD mode has no restrictions — the tool executes normally."""
    from durin.agent.agent_mode import BUILD_MODE
    from durin.agent.runner import AgentRunSpec, AgentRunner

    tools = ToolRegistry()
    tool = _BenignTool(name="edit_file")
    tools.register(tool)
    runner = AgentRunner(MagicMock())
    seen: set[str] = set()

    spec = AgentRunSpec(
        initial_messages=[],
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        mode_provider=lambda: BUILD_MODE,
    )

    results, _, _ = await runner._execute_tools(
        spec,
        [ToolCallRequest(id="c1", name="edit_file", arguments={})],
        {}, {}, seen,
    )
    assert tool.call_count == 1
    assert results[0] == "ok"
