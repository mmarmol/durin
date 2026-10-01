"""The task-state block follows the turn.

Decisions and findings the model records with note_decision (and its todo
list) are rendered into the prompt when the turn starts. During a long turn,
older tool results get trimmed from the context, so a finding recorded
mid-turn must reach the next request too. It is appended at the end of that
request only: never saved into history, never repeated when unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.providers.base import LLMResponse, ToolCallRequest
from durin.utils.runtime import FINALIZATION_RETRY_PROMPT

BASE = ["<task-state>", "## Decisions & findings", "- rule 99ce3a72 edited at 21:16", "</task-state>"]
UPDATED = [
    "<task-state>",
    "## Decisions & findings",
    "- rule 99ce3a72 edited at 21:16",
    "- rule_executed is the proof a rule ran",
    "</task-state>",
]


async def _run(provider_lines: dict[str, list[str]], *, change_after_first_tool: bool) -> tuple[list[list[dict[str, Any]]], Any]:
    seen: list[list[dict[str, Any]]] = []
    calls = {"n": 0}

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        seen.append([dict(m) for m in kwargs["messages"]])
        calls["n"] += 1
        if calls["n"] <= 2:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id=f"call_{calls['n']}", name="probe", arguments={})],
                usage={},
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    async def execute(name: str, params: Any) -> str:
        if change_after_first_tool:
            provider_lines["lines"] = UPDATED
        return "ok"

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = execute
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go\n" + "\n".join(BASE)}],
        tools=tools,
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=16_000,
        task_state_provider=lambda: provider_lines["lines"],
    ))
    return seen, result


@pytest.mark.asyncio
async def test_a_finding_recorded_mid_turn_reaches_the_next_request() -> None:
    lines = {"lines": BASE}
    seen, result = await _run(lines, change_after_first_tool=True)

    last = seen[1][-1]
    assert last["role"] == "user"
    assert "- rule_executed is the proof a rule ran" in str(last["content"])
    # It is a view for the request, not part of the saved conversation.
    assert not any(
        "rule_executed is the proof" in str(m.get("content")) for m in result.messages
    )


@pytest.mark.asyncio
async def test_the_no_tools_finalization_request_ends_with_its_instruction() -> None:
    lines = {"lines": BASE}
    requests: list[tuple[list[dict[str, Any]], Any]] = []
    calls = {"n": 0}

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        requests.append(([dict(m) for m in kwargs["messages"]], kwargs["tools"]))
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="call_1", name="probe", arguments={})],
                usage={},
            )
        if calls["n"] <= 3:
            # Blank answers: retried once, then the no-tools finalization.
            return LLMResponse(content="", tool_calls=[], usage={})
        return LLMResponse(content="done", tool_calls=[], usage={})

    async def execute(name: str, params: Any) -> str:
        lines["lines"] = UPDATED
        return "ok"

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = [
        {"type": "function", "function": {"name": "probe", "parameters": {"type": "object", "properties": {}}}},
    ]
    tools.execute = execute
    await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go\n" + "\n".join(BASE)}],
        tools=tools,
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=16_000,
        task_state_provider=lambda: lines["lines"],
    ))

    working, working_tools = requests[1]
    assert working_tools
    assert "- rule_executed is the proof a rule ran" in str(working[-1]["content"])
    final, final_tools = requests[-1]
    assert final_tools is None
    assert str(final[-1]["content"]).endswith(FINALIZATION_RETRY_PROMPT)


@pytest.mark.asyncio
async def test_an_unchanged_task_state_adds_nothing() -> None:
    lines = {"lines": BASE}
    seen, _ = await _run(lines, change_after_first_tool=False)

    for request in seen:
        user_messages = [m for m in request if m.get("role") == "user"]
        assert len(user_messages) == 1


async def _run_rewriting_decisions(block_limit: int | None) -> tuple[Any, list[int], list[int]]:
    """A run whose every tool call rewrites the decision log, as note_decision
    does: each request appends a new task state of about the same length.
    The provider reports, as each request's usage, the count of what it was
    sent. Returns the result, the precheck's estimate of each request and
    the size of each request sent."""
    from durin.utils.helpers import estimate_prompt_tokens

    lines = {"lines": BASE}
    sent: list[int] = []
    estimates: list[int] = []
    calls = {"n": 0}

    async def chat_with_retry(*, messages: Any, tools: Any = None, **_kwargs: Any) -> LLMResponse:
        calls["n"] += 1
        tokens = estimate_prompt_tokens(messages, tools)
        sent.append(tokens)
        usage = {"prompt_tokens": tokens, "completion_tokens": 5}
        if calls["n"] <= 5:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id=f"call_{calls['n']}", name="note", arguments={})],
                usage=usage,
            )
        return LLMResponse(content="done", tool_calls=[], usage=usage)

    async def execute(name: str, params: Any) -> str:
        lines["lines"] = [
            "<task-state>",
            "## Decisions & findings",
            f"- decision {calls['n']}: " + "we chose the second approach because it is simpler. " * 50,
            "</task-state>",
        ]
        return "noted"

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = [
        {"type": "function", "function": {
            "name": "note", "description": "record a decision",
            "parameters": {"type": "object", "properties": {}},
        }},
    ]
    tools.execute = execute
    runner = AgentRunner(provider)
    real_estimate = runner._estimate_and_budget

    def _spy(spec: Any, messages: Any, prov: Any = None) -> Any:
        decision = real_estimate(spec, messages, prov)
        if decision is not None:
            estimates.append(decision[0])
        return decision

    runner._estimate_and_budget = _spy  # type: ignore[method-assign]
    result = await runner.run(AgentRunSpec(
        initial_messages=[
            {"role": "system", "content": "You are a test. " * 300},
            {"role": "user", "content": "go\n" + "\n".join(BASE)},
        ],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=16_000,
        context_window_tokens=200_000,
        context_block_limit=block_limit,
        task_state_provider=lambda: lines["lines"],
    ))
    return result, estimates, sent


@pytest.mark.asyncio
async def test_every_request_is_estimated_as_sent_while_the_task_state_changes() -> None:
    """The precheck estimates a request from the provider's count of the
    previous one (its usage stamp) plus what came after it. That count
    already held the task state the previous request appended, and the new
    request appends the current one: counting both put every request after
    the first change a whole task state over what was sent."""
    result, estimates, sent = await _run_rewriting_decisions(None)

    assert result.stop_reason == "completed"
    assert len(sent) == len(estimates) == 6
    over_by = [estimate - size for estimate, size in zip(estimates, sent)]
    assert all(abs(delta) <= 1 for delta in over_by), over_by


@pytest.mark.asyncio
async def test_a_run_that_fits_its_budget_is_not_stopped_by_its_task_state() -> None:
    """A block limit a little above the largest request, less than one task
    state above it: the run fits, so the precheck must let every request
    through."""
    _, _, sent = await _run_rewriting_decisions(None)

    result, _, sent_again = await _run_rewriting_decisions(max(sent) + 200)

    assert result.stop_reason == "completed"
    assert sent_again == sent
