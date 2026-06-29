"""Tests for SubagentManager."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.runner import AgentRunResult
from durin.agent.subagent import SubagentManager
from durin.bus.queue import MessageBus
from durin.providers.base import LLMProvider


@pytest.mark.asyncio
async def test_subagent_uses_tool_loader():
    """Verify subagent registers tools via ToolLoader, not hard-coded imports."""
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=Path("/tmp"),
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
    )
    tools = sm._build_tools()
    assert tools.has("read_file")
    assert tools.has("write_file")
    assert not tools.has("message")
    assert not tools.has("spawn")


@pytest.mark.asyncio
async def test_subagent_build_tools_isolates_file_read_state(tmp_path):
    """Each spawned subagent needs a fresh file-state cache."""
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
    )

    first_read = sm._build_tools().get("read_file")
    second_read = sm._build_tools().get("read_file")

    assert first_read is not second_read
    assert (await first_read.execute(path="note.txt")).startswith("1| hello")
    second_result = await second_read.execute(path="note.txt")
    assert second_result.startswith("1| hello")
    assert "File unchanged" not in second_result


@pytest.mark.asyncio
async def test_subagent_run_enables_concurrent_tools(tmp_path):
    """Subagents run independent concurrency-safe tools in parallel, like
    the main loop, so the run spec must opt in to concurrent execution."""
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
    )
    sm.runner.run = AsyncMock(return_value=AgentRunResult(
        final_content="done", messages=[], stop_reason="completed",
    ))
    await sm.spawn("task")
    await asyncio.sleep(0.1)
    sm.runner.run.assert_awaited_once()
    spec = sm.runner.run.await_args.args[0]
    assert spec.concurrent_tools is True


def test_subagent_prompt_instructs_parallel_tool_calls(tmp_path):
    """The subagent system prompt instructs emitting independent tool calls
    together so the runner's parallel execution gets multi-call turns."""
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    sm = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
    )
    prompt = sm._build_subagent_prompt()
    assert "run in parallel" in prompt
