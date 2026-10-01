"""/compact and the record /new files summarize a whole conversation at once:
one larger than what a single summarizing call may take is summarized in
several calls, none of them cut."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import tiktoken

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.command.router import CommandContext
from durin.providers.base import GenerationSettings, LLMResponse
from durin.utils.prompt_templates import render_template

KEY = "cli:direct"


def _loop(tmp_path: Path) -> tuple[AgentLoop, list[str]]:
    """A loop on an 8,192-token model, whose summarizing calls take about
    6,000 tokens each; returns it with the inputs of those calls."""
    archive_prompt = render_template("agent/consolidator_archive.md", strip=True)
    inputs: list[str] = []
    calls = {"n": 0}

    async def _chat(*_args, messages=None, tools=None, **_kwargs):
        calls["n"] += 1
        if not tools and messages and messages[0].get("content") == archive_prompt:
            inputs.append(messages[-1]["content"])
        return LLMResponse(content=f"- summary {calls['n']}")

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=1024)
    provider.chat_with_retry = _chat
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model",
        context_window_tokens=8_192,
    )
    return loop, inputs


def _long_conversation(loop: AgentLoop) -> list[dict[str, Any]]:
    session = loop.sessions.get_or_create(KEY)
    for i in range(20):
        session.add_message("user", f"question {i} " + "words " * 500)
        session.add_message("assistant", f"answer {i}")
    loop.sessions.save(session)
    return [dict(m) for m in session.messages]


def _assert_summarized_whole(loop: AgentLoop, inputs: list[str], conversation: list[dict[str, Any]]) -> None:
    budget = loop.consolidator._input_token_budget
    encoding = tiktoken.get_encoding("cl100k_base")
    assert len(inputs) > 1
    for text in inputs:
        assert "... (truncated)" not in text
        assert len(encoding.encode(text)) <= budget
    summarized = "\n".join(inputs)
    assert [m["content"] for m in conversation if m["content"] not in summarized] == []


def _ctx(loop: AgentLoop, raw: str) -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="u", chat_id="direct", content=raw)
    return CommandContext(msg=msg, session=None, key=KEY, raw=raw, loop=loop)


@pytest.mark.asyncio
async def test_compact_summarizes_a_conversation_larger_than_one_call_whole(tmp_path: Path) -> None:
    from durin.command.builtin import cmd_compact

    loop, inputs = _loop(tmp_path)
    conversation = _long_conversation(loop)

    out = await cmd_compact(_ctx(loop, "/compact"))

    assert "Compacted 40 messages" in out.content
    _assert_summarized_whole(loop, inputs, conversation)


@pytest.mark.asyncio
async def test_new_files_a_conversation_larger_than_one_call_whole(tmp_path: Path) -> None:
    from durin.command.builtin import cmd_new
    from durin.memory.session_summary_store import closed_record_key, read_session_summary_entry

    loop, inputs = _loop(tmp_path)
    conversation = _long_conversation(loop)
    last_active = loop.sessions.get_or_create(KEY).updated_at
    scheduled: list[Any] = []
    loop._schedule_background = scheduled.append  # type: ignore[method-assign]

    await cmd_new(_ctx(loop, "/new"))
    for coro in scheduled:
        await coro

    _assert_summarized_whole(loop, inputs, conversation)
    record = read_session_summary_entry(loop.workspace, closed_record_key(KEY, last_active))
    assert record is not None
    assert all(f"- summary {n}" in (record.body or record.summary) for n in range(1, len(inputs) + 1))
