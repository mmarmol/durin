"""The turn that compacts must see the summary of what it just archived.

The state machine reads the pending summary in COMPACT and runs the token
consolidation in BUILD. When that consolidation archived turns and wrote the
session summary, the prompt was still built from the summary read before it
ran — ``None`` on a first compaction — so the model saw neither the archived
messages (the history is re-derived after the consolidation) nor their
summary. Found live: a turn whose ``compaction.completed`` row preceded its
``context.composition`` row by twenty seconds had no ``session_summary``
block in its volatile layer.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.memory.session_summary_store import write_session_summary
from durin.providers.base import GenerationSettings, LLMResponse


def _make_loop(tmp_path: Path) -> tuple[AgentLoop, dict]:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (0, "test-counter")
    captured: dict = {}

    async def _chat(*args, messages=None, **kwargs):
        captured["messages"] = messages
        return LLMResponse(content="ok", tool_calls=[])

    provider.chat_with_retry = _chat
    provider.chat_stream_with_retry = _chat
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]
    return loop, captured


@pytest.mark.asyncio
async def test_the_compacting_turn_sees_the_summary_it_just_wrote(tmp_path: Path) -> None:
    loop, captured = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "the port is 18992", "timestamp": "2026-09-21T10:00:00"},
        {"role": "assistant", "content": "noted", "timestamp": "2026-09-21T10:00:01"},
    ]
    loop.sessions.save(session)

    async def _consolidate(sess, *, replay_max_messages=None):
        # What a real consolidation does to the session: the turns leave the
        # history and their summary lands in the store.
        write_session_summary(
            tmp_path, sess.key, "- the port is 18992", last_active=date(2026, 9, 21),
        )
        sess.messages = []

    loop.consolidator.maybe_consolidate_by_tokens = _consolidate  # type: ignore[method-assign]

    await loop.process_direct("which port?", session_key="cli:test")

    system_prompt = captured["messages"][0]["content"]
    assert "the port is 18992" not in "".join(
        m["content"] for m in captured["messages"][1:] if isinstance(m.get("content"), str)
    ), "the archived turn should have left the history"
    # The archived-summary block, not only the memory layer's rendering of the
    # same summary file: the block is what carries the archive footer.
    assert "=== ARCHIVED SUMMARY" in system_prompt
    assert "- the port is 18992" in system_prompt
    assert "session_search(" in system_prompt
