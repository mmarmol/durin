"""Iteration-0 overflow recovery: force consolidation + retry the turn once.

The consolidator's input budget is structurally tighter than the runner's
(it reserves the full completion ceiling; the runner reserves a capped one),
so a *successful* BUILD-time consolidation always produces a context the
runner accepts. An overflow before ANY tool ran therefore means BUILD's
consolidation FAILED (e.g. compaction lock timeout). ``_state_run`` recovers
by forcing a fresh consolidation, rebuilding the context, and re-running the
turn once — but only when no tool has executed yet, since re-running would
re-fire side-effecting tools.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop, TurnContext, TurnState
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")


def _ctx(loop: AgentLoop) -> TurnContext:
    msg = InboundMessage(channel="websocket", sender_id="u", chat_id="c", content="hi")
    ctx = TurnContext(msg=msg, session_key="c", state=TurnState.RUN, turn_id="t1")
    ctx.session = MagicMock()
    # The retry re-reads the archived summary for the session, which needs a
    # real key and metadata; a bare mock's attributes are not strings.
    ctx.session.key = "websocket:c"
    ctx.session.metadata = {}
    ctx.session.messages = []
    ctx.session.get_history.return_value = []
    return ctx


_OVERFLOW_NO_TOOLS = ("Error: prompt overflow before LLM call.", [], [], "mid_turn_precheck_overflow", False, [])
_SUCCESS = ("Done.", ["read_file"], [{"role": "assistant", "content": "Done."}], "completed", False, [])
_OVERFLOW_WITH_TOOLS = ("Error: prompt overflow.", ["exec"], [], "mid_turn_precheck_overflow", False, [])


@pytest.mark.asyncio
async def test_iteration0_overflow_forces_consolidation_and_recovers(tmp_path, monkeypatch):
    from datetime import date

    from durin.memory.session_summary_store import write_session_summary

    loop = _make_loop(tmp_path)
    loop._run_agent_loop = AsyncMock(side_effect=[_OVERFLOW_NO_TOOLS, _SUCCESS])

    async def _consolidate(session, **kwargs):
        # The forced consolidation archives turns and writes their summary.
        write_session_summary(tmp_path, session.key, "- archived by the retry", last_active=date(2026, 9, 21))

    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=_consolidate)
    loop._build_initial_messages = MagicMock(return_value=[])
    monkeypatch.setattr("durin.agent.loop.publish_turn_run_status", AsyncMock())

    ctx = _ctx(loop)
    await loop._state_run(ctx)

    assert loop._run_agent_loop.await_count == 2, "must retry once after forced consolidation"
    loop.consolidator.maybe_consolidate_by_tokens.assert_awaited_once()
    assert ctx.stop_reason == "completed"
    assert ctx.final_content == "Done."
    # The rebuilt prompt carries the summary the forced consolidation just
    # wrote, not the one read before the turn started.
    rebuilt_summary = loop._build_initial_messages.call_args.args[3]
    assert rebuilt_summary is not None and "- archived by the retry" in rebuilt_summary


@pytest.mark.asyncio
async def test_overflow_after_tools_does_not_retry(tmp_path, monkeypatch):
    """Re-running would re-execute side-effecting tools, so an overflow that
    happened AFTER a tool ran must surface, not retry."""
    loop = _make_loop(tmp_path)
    loop._run_agent_loop = AsyncMock(side_effect=[_OVERFLOW_WITH_TOOLS])
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock()
    monkeypatch.setattr("durin.agent.loop.publish_turn_run_status", AsyncMock())

    ctx = _ctx(loop)
    await loop._state_run(ctx)

    assert loop._run_agent_loop.await_count == 1, "must NOT retry once tools have run"
    loop.consolidator.maybe_consolidate_by_tokens.assert_not_awaited()
    assert ctx.stop_reason == "mid_turn_precheck_overflow"


@pytest.mark.asyncio
async def test_retry_is_bounded_when_overflow_persists(tmp_path, monkeypatch):
    """If overflow persists even after the forced consolidation, give up after
    one retry (no infinite loop) and surface the error."""
    loop = _make_loop(tmp_path)
    loop._run_agent_loop = AsyncMock(side_effect=[_OVERFLOW_NO_TOOLS, _OVERFLOW_NO_TOOLS])
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock()
    loop._build_initial_messages = MagicMock(return_value=[])
    monkeypatch.setattr("durin.agent.loop.publish_turn_run_status", AsyncMock())

    ctx = _ctx(loop)
    await loop._state_run(ctx)

    assert loop._run_agent_loop.await_count == 2, "bounded to one retry"
    assert ctx.stop_reason == "mid_turn_precheck_overflow"
