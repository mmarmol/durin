"""A finished subagent persists its session with lineage (no orphaning)."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.runner import AgentRunResult
from durin.agent.subagent import SubagentManager, SubagentStatus
from durin.bus.queue import MessageBus
from durin.providers.base import LLMProvider
from durin.session import lineage
from durin.session.manager import Session, SessionManager


def _manager(tmp_path, sessions):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    return SubagentManager(
        provider=provider, workspace=tmp_path, bus=MessageBus(),
        model="test-model", max_tool_result_chars=16_000, sessions=sessions,
    )


@pytest.mark.asyncio
async def test_finished_subagent_persists_session_to_disk_with_lineage(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    sm = _manager(tmp_path, sessions)
    origin = {"channel": "websocket", "chat_id": "abc", "session_key": "websocket:abc"}
    status = SubagentStatus(
        task_id="t1", label="probe", task_description="investigate X",
        started_at=time.monotonic(), session_key="websocket:abc",
    )
    fake = AgentRunResult(
        final_content="found it",
        messages=[
            {"role": "user", "content": "investigate X"},
            {"role": "assistant", "content": "found it"},
        ],
    )
    with patch.object(sm.runner, "run", AsyncMock(return_value=fake)), \
         patch.object(sm, "_announce_result", AsyncMock()):
        await sm._run_subagent("t1", "investigate X", "probe", origin, status)

    # Reload from a COLD manager to prove it hit disk (the orphaning fix).
    reloaded = SessionManager(workspace=tmp_path).get_or_create("subagent:t1")
    assert lineage.parent_of(reloaded.metadata) == "websocket:abc"
    assert reloaded.metadata[lineage.ROOT_ID] == "websocket:abc"
    assert reloaded.metadata[lineage.ORIGIN_TYPE] == "subagent"
    assert reloaded.metadata[lineage.ORIGIN_ID] == "t1"
    assert any(m.get("content") == "found it" for m in reloaded.messages)


@pytest.mark.asyncio
async def test_nested_subagent_inherits_root(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    # The parent is itself a branch with a root two levels up.
    parent = Session(key="subagent:p0")
    parent.metadata.update(lineage.build_lineage(
        parent_session_id="websocket:abc", root_id="websocket:abc",
        origin_type="subagent", origin_id="p0",
    ))
    sessions.save(parent)
    sm = _manager(tmp_path, sessions)
    origin = {"channel": "cli", "chat_id": "direct", "session_key": "subagent:p0"}
    status = SubagentStatus(
        task_id="c1", label="child", task_description="t",
        started_at=time.monotonic(), session_key="subagent:p0",
    )
    fake = AgentRunResult(final_content="ok", messages=[{"role": "assistant", "content": "ok"}])
    with patch.object(sm.runner, "run", AsyncMock(return_value=fake)), \
         patch.object(sm, "_announce_result", AsyncMock()):
        await sm._run_subagent("c1", "t", "child", origin, status)

    child = SessionManager(workspace=tmp_path).get_or_create("subagent:c1")
    assert child.metadata[lineage.ROOT_ID] == "websocket:abc"  # inherited, not the immediate parent


@pytest.mark.asyncio
async def test_no_persist_without_session_manager(tmp_path):
    # When SubagentManager has no SessionManager, it must not raise.
    sm = _manager(tmp_path, sessions=None)
    origin = {"channel": "cli", "chat_id": "direct", "session_key": None}
    status = SubagentStatus(
        task_id="t2", label="x", task_description="t",
        started_at=time.monotonic(), session_key=None,
    )
    fake = AgentRunResult(final_content="ok", messages=[{"role": "assistant", "content": "ok"}])
    with patch.object(sm.runner, "run", AsyncMock(return_value=fake)), \
         patch.object(sm, "_announce_result", AsyncMock()):
        await sm._run_subagent("t2", "t", "x", origin, status)  # must not raise
