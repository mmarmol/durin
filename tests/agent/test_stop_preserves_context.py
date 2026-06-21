"""Tests for /stop preserving partial context from interrupted turns.

When /stop cancels an active task, the runtime checkpoint (tool results,
assistant messages accumulated so far) should be materialized into session
history rather than silently discarded.

See: https://github.com/HKUDS/durin/issues/2966
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus


@asynccontextmanager
async def _noop_lease(path: object, **kwargs: object) -> AsyncIterator[None]:
    """Stand-in for session_turn_lease that avoids MagicMock path flowing into
    cross_process_lock and creating stray ``.lock`` files in the CWD."""
    yield


def _make_provider():
    """Create an LLM provider mock with required attributes."""
    from types import SimpleNamespace
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    return provider


def _make_loop(tmp_path: Path) -> AgentLoop:
    """Create a real AgentLoop with mocked provider — avoids patching __init__."""
    bus = MessageBus()
    provider = _make_provider()
    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager") as MockSubMgr:
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        return AgentLoop(bus=bus, provider=provider, workspace=tmp_path)


class TestStopPreservesContext:
    """Verify that /stop restores partial context via checkpoint."""

    # Note: a `hasattr(_restore_runtime_checkpoint)` smoke test and a
    # `_RUNTIME_CHECKPOINT_KEY == "runtime_checkpoint"` constant test used
    # to live here. They were tautological — both are exercised end-to-end
    # by test_cancel_dispatch_restores_checkpoint below (which calls the
    # method and asserts the literal key is consumed), so they added no
    # coverage and were removed (QA review P3).

    def test_cancel_dispatch_restores_checkpoint(self, tmp_path):
        """When a task is cancelled, the checkpoint should be restored."""
        loop = _make_loop(tmp_path)
        session = MagicMock()
        session.metadata = {
            "runtime_checkpoint": {
                "phase": "awaiting_tools",
                "iteration": 0,
                "assistant_message": {
                    "role": "assistant",
                    "content": "Let me search for that.",
                    "tool_calls": [{"id": "tc_1", "type": "function",
                                    "function": {"name": "web_search", "arguments": "{}"}}],
                },
                "completed_tool_results": [
                    {"role": "tool", "tool_call_id": "tc_1",
                     "content": "Search results: ..."},
                ],
                "pending_tool_calls": [],
            }
        }
        session.messages = [
            {"role": "user", "content": "Search for something"},
        ]
        loop.sessions.get_or_create.return_value = session

        restored = loop._restore_runtime_checkpoint(session)
        assert restored is True
        assert len(session.messages) > 1
        assert "runtime_checkpoint" not in session.metadata


@pytest.mark.asyncio
async def test_dispatch_cancellation_restores_checkpoint():
    """Regression for #2966: /stop interrupting _dispatch must materialize the
    in-flight runtime checkpoint into session.messages before the cancellation
    unwinds, so the next turn can see the partial work.

    This exercises the real _dispatch path (locks, pending queues, the
    CancelledError handler) rather than poking _restore_runtime_checkpoint in
    isolation, so a future refactor that drops the cancel-time restore is
    caught by CI instead of silently regressing.
    """
    from durin.bus.events import InboundMessage
    from durin.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager") as MockSubMgr:
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)

    checkpoint_key = loop._RUNTIME_CHECKPOINT_KEY
    session = SimpleNamespace(
        key="test:c1",
        metadata={
            checkpoint_key: {
                "phase": "awaiting_tools",
                "iteration": 0,
                "assistant_message": {
                    "role": "assistant",
                    "content": "Let me search.",
                    "tool_calls": [
                        {
                            "id": "tc_1",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": "{}"},
                        }
                    ],
                },
                "completed_tool_results": [
                    {"role": "tool", "tool_call_id": "tc_1", "content": "Search hit."},
                ],
                "pending_tool_calls": [],
            }
        },
        messages=[{"role": "user", "content": "Search for something"}],
    )

    loop.sessions.get_or_create = MagicMock(return_value=session)
    loop.sessions.save = MagicMock()

    async def _cancel(*_args, **_kwargs):
        raise asyncio.CancelledError()

    loop._process_message = _cancel

    msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="work")

    with pytest.raises(asyncio.CancelledError), \
         patch("durin.agent.loop.session_turn_lease", _noop_lease):
        await loop._dispatch(msg)

    roles = [m.get("role") for m in session.messages]
    assert roles == ["user", "assistant", "tool"], (
        "Expected the assistant message and completed tool result from the "
        f"interrupted turn to be materialized into session.messages; got {roles}"
    )
    assert checkpoint_key not in session.metadata, \
        "Checkpoint metadata should be cleared after restore"
    assert loop.sessions.save.called, \
        "Session should be persisted so the restored state survives process restart"
