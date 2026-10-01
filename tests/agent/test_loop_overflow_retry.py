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
from durin.agent.runner import _PERSISTED_OVERFLOW_PLACEHOLDER
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


# An iteration-0 overflow returns the prompt it was given ([] here) plus the
# runner's overflow placeholder, and nothing else.
_OVERFLOW_NO_TOOLS = (
    "Error: prompt overflow before LLM call.", [],
    [{"role": "assistant", "content": _PERSISTED_OVERFLOW_PLACEHOLDER}],
    "mid_turn_precheck_overflow", False, [],
)
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
    # Forced: the overflow proves the prompt does not fit, so no deferral may
    # skip this consolidation.
    assert loop.consolidator.maybe_consolidate_by_tokens.await_args.kwargs.get("force") is True
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


@pytest.mark.asyncio
async def test_an_attempt_that_took_a_queued_message_is_not_rerun(tmp_path):
    """The retry rebuilds the turn from scratch, so it is only safe for an
    attempt that stopped before its first model call. One that answered, took
    a queued message into the turn and overflowed on its next call must not
    be re-run: the queued message is off its queue and would be lost, and the
    answer given twice. The turn ends on the overflow, with the answer and the
    queued message saved."""
    import dataclasses

    from durin.agent.loop import PendingQueues
    from durin.providers.base import GenerationSettings, LLMResponse
    from durin.utils.helpers import estimate_prompt_tokens
    from durin.utils.runtime import NO_ROOM_PLACEHOLDER

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=8192)
    provider.estimate_prompt_tokens.return_value = (0, "none")
    calls = {"n": 0}

    async def _chat(*_args, messages=None, tools=None, **_kwargs):
        calls["n"] += 1
        tokens = estimate_prompt_tokens(messages or [], tools or None)
        return LLMResponse(
            content=f"REPLY-{calls['n']} " + "Here is a reply. " * 40,
            usage={"prompt_tokens": max(1, tokens), "completion_tokens": 10},
        )

    provider.chat_with_retry = _chat
    provider.chat_stream_with_retry = _chat
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model",
        context_window_tokens=200_000,
    )
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]
    real_run = loop.runner.run
    runs: list[str] = []

    async def _run(spec):
        # The first call fits; the next, which also carries the answer and
        # the queued message, does not.
        tools = spec.tools.get_definitions() if spec.tools else None
        limit = estimate_prompt_tokens(spec.initial_messages, tools) + 300
        result = await real_run(dataclasses.replace(spec, context_block_limit=limit))
        runs.append(result.stop_reason)
        return result

    loop.runner.run = _run  # type: ignore[method-assign]
    queues = PendingQueues.create()
    queued = "a follow-up sent while the turn ran " + ("detail " * 300)
    queues.deferred.put_nowait(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=queued),
    )
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="the question")

    out = await loop._process_message(msg, session_key="cli:q", pending_queues=queues)

    assert runs == ["mid_turn_precheck_overflow"]
    assert out is not None and out.content.startswith("Error: prompt overflow")
    saved = loop.sessions.get_or_create("cli:q").messages
    assert [m["role"] for m in saved] == ["user", "assistant", "user", "assistant"]
    assert saved[1]["content"].startswith("REPLY-1 ")
    assert queued in saved[2]["content"]
    # The run's own messages are what is over the budget: the session has no
    # history a compaction could take out.
    assert saved[3]["content"] == NO_ROOM_PLACEHOLDER
