"""Old tool results leave the context in rare batches, and never come back.

Through a real run: the provider reports each request's size as its usage,
the way a real provider does, so the runner's size estimate is anchored on
what it actually sent — which is where a pruning rule that decides on one
list and sends another starts showing results that disappear and reappear.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.providers.base import LLMResponse, ToolCallRequest
from durin.utils.helpers import estimate_prompt_tokens

RESULT = "".join(str(i % 10) for i in range(1_500))
# The message fields a provider puts on the wire (the widest set, the
# OpenAI-compatible one). Bookkeeping such as usage stamps never leaves the
# runner, so it is not part of what the provider's prompt cache sees.
_SENT_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content", "extra_content")


def _sent(message: dict[str, Any]) -> dict[str, Any]:
    return {k: message[k] for k in _SENT_KEYS if k in message}


class _Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def log(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))


async def _run(
    initial: list[dict[str, Any]],
    calls: int,
    tmp_path,
    *,
    window: int,
    results: dict[int, str] | None = None,
) -> list[list[dict[str, Any]]]:
    """Run ``calls`` tool rounds; round n's result is ``results[n]`` or RESULT."""
    seen: list[list[dict[str, Any]]] = []
    rounds = {"n": 0}

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        messages = [_sent(m) for m in kwargs["messages"]]
        seen.append(messages)
        usage = {"prompt_tokens": estimate_prompt_tokens(messages), "completion_tokens": 5}
        n = len(seen)
        if n <= calls:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id=f"call_{n}", name="exec", arguments={"command": f"step {n}"})],
                usage=usage,
            )
        return LLMResponse(content="done", tool_calls=[], usage=usage)

    async def execute(name: str, params: Any) -> str:
        rounds["n"] += 1
        return (results or {}).get(rounds["n"], RESULT)

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = execute
    await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=initial,
        tools=tools,
        model="m",
        max_iterations=calls + 2,
        max_tool_result_chars=16_000,
        context_window_tokens=window,
        workspace=tmp_path,
        session_key="sess",
    ))
    return seen


def _fresh() -> list[dict[str, Any]]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]


def _rewrites(seen: list[list[dict[str, Any]]]) -> list[int]:
    """Requests that changed something the previous request already sent."""
    return [i for i in range(1, len(seen)) if seen[i][: len(seen[i - 1])] != seen[i - 1]]


@pytest.mark.asyncio
async def test_between_batches_each_request_extends_the_previous_one(tmp_path) -> None:
    seen = await _run(_fresh(), calls=70, tmp_path=tmp_path, window=40_000)
    rewrites = _rewrites(seen)
    # One batch near the limit frees enough that the rest of the run only
    # appends — the prompt cache stays warm for every other request.
    assert len(rewrites) == 1, rewrites


@pytest.mark.asyncio
async def test_a_pruned_result_never_comes_back(tmp_path) -> None:
    seen = await _run(_fresh(), calls=70, tmp_path=tmp_path, window=40_000)
    pruned: set[str] = set()
    for request in seen:
        for message in request:
            if message.get("role") != "tool":
                continue
            call_id = message["tool_call_id"]
            if "result trimmed" in str(message["content"]):
                pruned.add(call_id)
            else:
                assert call_id not in pruned, f"{call_id} reappeared in full after being pruned"
    assert pruned


@pytest.mark.asyncio
async def test_each_batch_is_recorded(tmp_path, monkeypatch) -> None:
    sink = _Sink()
    monkeypatch.setattr("durin.agent.runner.current_telemetry", lambda: sink)
    seen = await _run(_fresh(), calls=70, tmp_path=tmp_path, window=40_000)
    batches = [data for event, data in sink.events if event == "tool_results.pruned"]
    assert len(batches) == len(_rewrites(seen)) == 1
    batch = batches[0]
    assert batch["pruned_count"] >= 1
    assert batch["protected_count"] >= 1
    assert batch["freed_tokens"] >= 0.05 * batch["budget_tokens"]
    assert batch["estimated_tokens"] > 0.8 * batch["budget_tokens"]
    assert batch["session_key"] == "sess"


@pytest.mark.asyncio
async def test_a_batch_that_brings_the_prompt_back_under_the_budget_keeps_the_big_result_whole(tmp_path) -> None:
    """A large result pushes the prompt past the whole budget in one step; the
    batch prunes the old results and the prompt fits again. The size checks
    for that request must see the pruned prompt, not the last request's size
    plus the new result — or they cut the new result for nothing."""
    # Digits in a varying order: an alphabet run would tokenize to a few
    # hundred tokens and never press the budget.
    big = "".join(str((i * 7) % 10) for i in range(13_500))
    seen = await _run(_fresh(), calls=9, tmp_path=tmp_path, window=12_000, results={9: big})
    after = seen[9]
    contents = [str(m["content"]) for m in after if m.get("role") == "tool"]
    assert contents[-1] == big
    assert any("result trimmed" in c for c in contents[:-1])


def _stamped_history(count: int, stamp: int) -> list[dict[str, Any]]:
    """History from an earlier run that pruned: its last assistant message
    carries the small size of a pruned request, while this list holds every
    result in full (the way a run's result.messages does)."""
    history = _fresh()
    for i in range(count):
        history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"h{i}", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]})
        history.append({"role": "tool", "tool_call_id": f"h{i}", "name": "exec", "content": RESULT})
    history.append({"role": "assistant", "content": "ok", "usage_prompt_tokens": stamp})
    return history


@pytest.mark.asyncio
async def test_a_run_seeded_with_an_earlier_runs_messages_measures_them_from_scratch(tmp_path) -> None:
    """A workflow node's synthesis, re-entry and persistent revisit start from
    an earlier run's messages, stamps included. Those stamps measured that run's
    pruned requests; this run has pruned nothing yet, so trusting them would
    send the whole history unpruned — past the window."""
    history = _stamped_history(20, stamp=2_000) + [{"role": "user", "content": "go on"}]
    seen = await _run(history, calls=2, tmp_path=tmp_path, window=12_000)
    first = seen[0]
    assert any("result trimmed" in str(m.get("content")) for m in first)
    assert estimate_prompt_tokens(first) <= 12_000 - 4_096 - 1_024


@pytest.mark.asyncio
async def test_a_retry_after_a_batch_is_measured_on_the_pruned_prompt(tmp_path) -> None:
    """The batch request gets an empty reply, so the runner retries with no
    new stamped message. The retry must not be measured against the stamp of
    the request before the batch — that would cut the result the batch made
    room for."""
    big = "".join(str((i * 7) % 10) for i in range(13_500))
    seen: list[list[dict[str, Any]]] = []
    state = {"emptied": False, "reads": 0}

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        messages = [_sent(m) for m in kwargs["messages"]]
        seen.append(messages)
        usage = {"prompt_tokens": estimate_prompt_tokens(messages), "completion_tokens": 5}
        pruned_now = any("result trimmed" in str(m.get("content")) for m in messages)
        if pruned_now and not state["emptied"]:
            state["emptied"] = True
            return LLMResponse(content="", tool_calls=[], usage=usage)
        if state["reads"] < 9:
            state["reads"] += 1
            return LLMResponse(content="", tool_calls=[
                ToolCallRequest(id=f"call_{state['reads']}", name="exec", arguments={"n": state["reads"]})], usage=usage)
        return LLMResponse(content="done", tool_calls=[], usage=usage)

    executed = {"n": 0}

    async def execute(name: str, params: Any) -> str:
        executed["n"] += 1
        return big if executed["n"] == 9 else RESULT

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = execute
    await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=_fresh(), tools=tools, model="m", max_iterations=14,
        max_tool_result_chars=16_000, context_window_tokens=12_000, workspace=tmp_path, session_key="sess",
    ))
    assert state["emptied"], "the batch request never happened"
    for request in seen:
        for message in request:
            assert "truncated: context budget" not in str(message.get("content"))


@pytest.mark.asyncio
async def test_a_new_turn_is_measured_from_scratch_and_prunes_on_its_first_request(tmp_path) -> None:
    """A new turn's history carries no usage stamps (session history replays
    only role, content and tool fields), so its first request is measured
    whole and pruned right away when it is over the threshold."""
    history = _fresh()
    for i in range(20):
        history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"h{i}", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]})
        history.append({"role": "tool", "tool_call_id": f"h{i}", "name": "exec", "content": RESULT})
    history.append({"role": "assistant", "content": "ok"})
    history.append({"role": "user", "content": "go on"})
    seen = await _run(history, calls=2, tmp_path=tmp_path, window=12_000)
    assert any("result trimmed" in str(m.get("content")) for m in seen[0])
