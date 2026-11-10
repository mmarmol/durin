"""Subagents spawned in different minutes share a system-prompt prefix."""

import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.context import ContextBuilder
from durin.agent.runner import AgentRunResult
from durin.agent.skills import SkillsLoader
from durin.agent.subagent import SubagentManager, SubagentStatus
from durin.bus.queue import MessageBus
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.utils.prompt_templates import render_template


def _manager(tmp_path, sessions=None):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    return SubagentManager(
        provider=provider, workspace=tmp_path, bus=MessageBus(),
        model="test-model", max_tool_result_chars=16_000, sessions=sessions,
    )


def _clock(stamp):
    return patch("durin.agent.context.current_time_str", return_value=stamp)


def test_system_prompt_is_identical_across_minutes(tmp_path):
    sm = _manager(tmp_path)
    with _clock("2026-11-10 09:01 (Tuesday) (UTC, UTC+00:00)"):
        first = sm._build_initial_messages("look into A")
    with _clock("2026-11-10 09:42 (Tuesday) (UTC, UTC+00:00)"):
        second = sm._build_initial_messages("look into B")
    assert first[0]["role"] == "system"
    assert first[0]["content"] == second[0]["content"]


def test_system_prompt_has_no_rendered_time(tmp_path):
    sm = _manager(tmp_path)
    with _clock("2026-11-10 09:01 (Tuesday) (UTC, UTC+00:00)"):
        prompt = sm._build_subagent_prompt()
    assert "2026-11-10" not in prompt
    assert "Current Time" not in prompt
    assert ContextBuilder._RUNTIME_CONTEXT_TAG not in prompt


def test_task_message_carries_the_time_in_the_main_loop_format(tmp_path):
    sm = _manager(tmp_path)
    stamp = "2026-11-10 09:01 (Tuesday) (UTC, UTC+00:00)"
    with _clock(stamp):
        messages = sm._build_initial_messages("look into A")
        expected_block = ContextBuilder._build_runtime_context(None, None)
    user = messages[1]
    assert user["role"] == "user"
    assert user["content"] == f"look into A\n\n{expected_block}"
    assert f"Current Time: {stamp}" in user["content"]


def test_system_prompt_keeps_the_template_sections(tmp_path):
    sm = _manager(tmp_path)
    prompt = sm._build_subagent_prompt()
    expected = render_template(
        "agent/subagent_system.md",
        workspace=str(tmp_path),
        skills_summary=SkillsLoader(tmp_path).build_skills_summary(),
    )
    assert prompt == expected
    for section in ("# Subagent", "run in parallel", "## Workspace", str(tmp_path)):
        assert section in prompt
    assert re.search(r"untrusted external data", prompt)


@pytest.mark.asyncio
async def test_spawn_path_sends_stable_system_and_timed_task(tmp_path):
    sm = _manager(tmp_path)
    sm.runner.run = AsyncMock(return_value=AgentRunResult(
        final_content="done", messages=[], stop_reason="completed",
    ))
    origin = {"channel": "cli", "chat_id": "direct"}
    sent = []
    for stamp in ("2026-11-10 09:01 (Tuesday)", "2026-11-10 09:42 (Tuesday)"):
        status = SubagentStatus(
            task_id=stamp[-9:], label="l", task_description="t",
            started_at=time.monotonic(),
        )
        with _clock(stamp), patch.object(sm, "_announce_result", AsyncMock()):
            await sm._run_subagent("t", "same task", "l", origin, status)
        sent.append(sm.runner.run.await_args.args[0].initial_messages)
    assert sent[0][0] == sent[1][0]
    assert "09:01" in sent[0][1]["content"] and "09:42" in sent[1][1]["content"]
    assert all(m["content"].startswith("same task") for m in (sent[0][1], sent[1][1]))


@pytest.mark.asyncio
async def test_persisted_subagent_session_drops_the_time_block(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    sm = _manager(tmp_path, sessions)
    origin = {"channel": "cli", "chat_id": "direct", "session_key": "cli:direct"}
    status = SubagentStatus(
        task_id="p1", label="l", task_description="t",
        started_at=time.monotonic(), session_key="cli:direct",
    )

    async def fake_run(spec):
        return AgentRunResult(
            final_content="ok",
            messages=[*spec.initial_messages, {"role": "assistant", "content": "ok"}],
            stop_reason="completed",
        )

    sm.runner.run = fake_run
    with _clock("2026-11-10 09:01 (Tuesday)"), patch.object(sm, "_announce_result", AsyncMock()):
        await sm._run_subagent("p1", "investigate X", "l", origin, status)

    stored = SessionManager(workspace=tmp_path).get_or_create("subagent:p1").messages
    user = next(m for m in stored if m["role"] == "user")
    assert user["content"] == "investigate X"
    assert all("Runtime Context" not in str(m.get("content")) for m in stored)
