"""read_file's "unchanged since last read" stub points at an earlier result in
the model's context. It may only answer while that result is still there:
once a pruning batch takes it out of view, or a new turn starts from history
that may no longer hold it, a repeat read returns the content again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.agent.tools.filesystem import ReadFileTool
from durin.agent.tools.registry import ToolRegistry
from durin.bus.queue import MessageBus
from durin.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from durin.utils.helpers import estimate_prompt_tokens

FILLER = "".join(str(i % 10) for i in range(1_500))
_STUB = "File unchanged since last read"


def _file(tmp_path: Path) -> Path:
    f = tmp_path / "a.txt"
    f.write_text("\n".join(f"alpha line {i} of the file under test" for i in range(60)), encoding="utf-8")
    return f


async def _run(tmp_path: Path, plan: list[str], *, window: int) -> list[dict[str, Any]]:
    """Run one read_file call per entry of ``plan``: "a" reads the file under
    test, "other" reads a different file of its own, which fills the context
    so a pruning batch fires. Return the last request the model was sent."""
    target = str(_file(tmp_path))
    paths: list[str] = []
    for n, step in enumerate(plan, start=1):
        if step == "a":
            paths.append(target)
            continue
        other = tmp_path / f"other_{n}.txt"
        other.write_text(FILLER, encoding="utf-8")
        paths.append(str(other))
    seen: list[list[dict[str, Any]]] = []

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        messages = kwargs["messages"]
        seen.append(messages)
        usage = {"prompt_tokens": estimate_prompt_tokens(messages), "completion_tokens": 5}
        n = len(seen)
        if n > len(plan):
            return LLMResponse(content="done", tool_calls=[], usage=usage)
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"call_{n}", name="read_file", arguments={"path": paths[n - 1]})],
            usage=usage,
        )

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        tools=tools,
        model="m",
        max_iterations=len(plan) + 2,
        max_tool_result_chars=16_000,
        context_window_tokens=window,
        workspace=tmp_path,
        session_key="sess",
    ))
    return seen[-1]


def _result(request: list[dict[str, Any]], call_id: str) -> str:
    return next(str(m["content"]) for m in request if m.get("tool_call_id") == call_id)


@pytest.mark.asyncio
async def test_a_read_pruned_from_view_is_read_again_in_full(tmp_path: Path) -> None:
    plan = ["a", *(["other"] * 70), "a"]

    last = await _run(tmp_path, plan, window=40_000)

    assert "result trimmed" in _result(last, "call_1"), "the batch should have pruned the first read"
    again = _result(last, f"call_{len(plan)}")
    assert _STUB not in again
    assert "alpha line 3 of the file" in again


@pytest.mark.asyncio
async def test_a_read_still_in_view_keeps_its_stub(tmp_path: Path) -> None:
    last = await _run(tmp_path, ["a", "a"], window=40_000)

    assert "alpha line 3 of the file" in _result(last, "call_1")
    assert _STUB in _result(last, "call_2")


@pytest.mark.asyncio
async def test_a_turn_after_compaction_reads_the_file_again(tmp_path: Path) -> None:
    target = str(_file(tmp_path))
    requests: list[list[dict[str, Any]]] = []

    async def _chat(*args: Any, messages: Any = None, **kwargs: Any) -> LLMResponse:
        # A copy: the list handed to the provider is the run's own, and it
        # keeps growing after the call.
        requests.append([dict(m) for m in messages])
        if len(requests) in (1, 3):
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id=f"read_{len(requests)}", name="read_file", arguments={"path": target},
                )],
            )
        return LLMResponse(content="ok", tool_calls=[])

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (0, "test-counter")
    provider.chat_with_retry = _chat
    provider.chat_stream_with_retry = _chat
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]

    async def _keep(sess: Any, *, replay_max_messages: Any = None) -> None:
        return None

    async def _archive(sess: Any, *, replay_max_messages: Any = None) -> None:
        # What a real consolidation does: the earlier turns leave the history.
        sess.messages = []

    loop.consolidator.maybe_consolidate_by_tokens = _keep  # type: ignore[method-assign]
    await loop.process_direct("read a.txt", session_key="cli:dedup")
    loop.consolidator.maybe_consolidate_by_tokens = _archive  # type: ignore[method-assign]
    await loop.process_direct("read a.txt again", session_key="cli:dedup")

    assert not any(
        "alpha line 3 of the file" in str(m.get("content"))
        for m in requests[2]
    ), "the first turn's read should have left the history"
    again = _result(requests[3], "read_3")
    assert _STUB not in again
    assert "alpha line 3 of the file" in again
