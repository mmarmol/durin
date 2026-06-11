"""Tests for the ``ask_user_question`` tool."""

from __future__ import annotations

import asyncio

import pytest

from durin.agent.tools.ask_user import PENDING_QUESTION_KEY, AskUserQuestionTool
from durin.agent.tools.context import RequestContext
from durin.session.manager import SessionManager


def _tool(sm: SessionManager) -> AskUserQuestionTool:
    # blocking=False: these tests exercise the V1 yield contract; the
    # blocking V2 path has its own tests below.
    t = AskUserQuestionTool(sessions=sm, blocking=False)
    rc = RequestContext(
        channel="cli",
        chat_id="d",
        session_key="cli:d",
        metadata={},
    )
    t.set_context(rc)
    return t


@pytest.mark.asyncio
async def test_records_pending_question_on_session_metadata(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)

    out = await tool.execute(question="Which framework?")

    assert "presented to the user" in out
    assert "STOP" in out
    assert "do not repeat" in out.lower()
    assert "Which framework?" in out

    sess = sm.get_or_create("cli:d")
    pending = sess.metadata.get(PENDING_QUESTION_KEY)
    assert isinstance(pending, dict)
    assert pending["question"] == "Which framework?"
    assert pending["options"] == []
    assert isinstance(pending["question_id"], str) and pending["question_id"]


@pytest.mark.asyncio
async def test_options_are_included_when_provided(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)

    out = await tool.execute(
        question="Pick one",
        options=["React", "Vue", "Svelte"],
    )

    assert "Suggested options" in out
    assert "React" in out and "Vue" in out and "Svelte" in out

    sess = sm.get_or_create("cli:d")
    assert sess.metadata[PENDING_QUESTION_KEY]["options"] == [
        "React", "Vue", "Svelte",
    ]


@pytest.mark.asyncio
async def test_options_with_single_item_are_dropped(tmp_path):
    """A 'list' with only one viable answer is degenerate — treat as no
    options to avoid presenting a fake choice."""
    sm = SessionManager(tmp_path)
    tool = _tool(sm)

    await tool.execute(question="Pick one", options=["Only one"])

    sess = sm.get_or_create("cli:d")
    assert sess.metadata[PENDING_QUESTION_KEY]["options"] == []


@pytest.mark.asyncio
async def test_options_get_clamped_to_six(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)

    huge = [f"opt{i}" for i in range(20)]
    await tool.execute(question="Pick one", options=huge)

    sess = sm.get_or_create("cli:d")
    stored = sess.metadata[PENDING_QUESTION_KEY]["options"]
    assert len(stored) == 6


@pytest.mark.asyncio
async def test_rejects_empty_question(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)

    out = await tool.execute(question="   ")
    assert "Error" in out

    out2 = await tool.execute()  # question missing
    assert "Error" in out2


@pytest.mark.asyncio
async def test_each_call_assigns_a_unique_question_id(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)

    await tool.execute(question="First?")
    id1 = sm.get_or_create("cli:d").metadata[PENDING_QUESTION_KEY]["question_id"]

    await tool.execute(question="Second?")
    id2 = sm.get_or_create("cli:d").metadata[PENDING_QUESTION_KEY]["question_id"]

    assert id1 != id2
    # Latest question wins (single-pending-question contract for V1).
    assert sm.get_or_create("cli:d").metadata[PENDING_QUESTION_KEY]["question"] == "Second?"


@pytest.mark.asyncio
async def test_no_session_context_errors_gracefully(tmp_path):
    sm = SessionManager(tmp_path)
    tool = AskUserQuestionTool(sessions=sm)
    # No request context set ⇒ no session resolution; the tool must
    # still return a useful result instead of crashing.
    out = await tool.execute(question="Does this still respond?")
    # Falls through: no session metadata written, but the yield message
    # is still returned so the model knows what to do.
    assert "presented to the user" in out
    assert "STOP" in out


@pytest.mark.asyncio
async def test_tool_is_in_plan_mode_allowed_set():
    """Asking a question is read-safe; it must work in plan mode."""
    from durin.agent.agent_mode import PLAN_MODE

    assert PLAN_MODE.is_tool_allowed("ask_user_question")


def test_tool_discovered_by_loader():
    """The auto-discovery loader picks up the new tool class."""
    from durin.agent.tools.loader import ToolLoader

    loader = ToolLoader()
    names = [c.__name__ for c in loader.discover()]
    assert "AskUserQuestionTool" in names


# ---------------------------------------------------------------------------
# Blocking in-turn answers (V2)
# ---------------------------------------------------------------------------


def _blocking_tool(sm: SessionManager, *, timeout_s: float = 5.0) -> AskUserQuestionTool:
    from durin.agent import pending_answers as _pa

    t = AskUserQuestionTool(sessions=sm, blocking=True, answer_timeout_s=timeout_s)
    t.set_context(RequestContext(channel="cli", chat_id="d", session_key="cli:d", metadata={}))
    # Blocking only engages while the loop consumer is alive (can_block).
    _pa.set_consumer_active(True)
    return t


@pytest.mark.asyncio
async def test_blocking_returns_user_answer_in_same_turn(tmp_path):
    from durin.agent import pending_answers as pa

    pa.reset()
    sm = SessionManager(tmp_path)
    tool = _blocking_tool(sm)

    async def _answer_soon():
        for _ in range(100):
            await asyncio.sleep(0.01)
            if pa.is_waiting("cli:d"):
                assert pa.resolve("cli:d", "green") is True
                return
        raise AssertionError("tool never registered a waiter")

    answer_task = asyncio.create_task(_answer_soon())
    out = await tool.execute(question="Which color?")
    await answer_task

    assert "green" in out
    assert "answered" in out.lower()
    # The interaction is consumed: no pending question survives.
    sess = sm.get_or_create("cli:d")
    assert PENDING_QUESTION_KEY not in sess.metadata
    pa.reset()


@pytest.mark.asyncio
async def test_blocking_times_out_to_yield_semantics(tmp_path):
    from durin.agent import pending_answers as pa

    pa.reset()
    sm = SessionManager(tmp_path)
    tool = _blocking_tool(sm, timeout_s=0.05)

    out = await tool.execute(question="Anyone there?")

    # Degrades to the V1 contract: question stays pending for the next turn.
    assert "presented to the user" in out
    assert "STOP" in out
    sess = sm.get_or_create("cli:d")
    assert sess.metadata[PENDING_QUESTION_KEY]["question"] == "Anyone there?"
    pa.reset()


@pytest.mark.asyncio
async def test_blocking_fallback_sentinel_yields(tmp_path):
    from durin.agent import pending_answers as pa

    pa.reset()
    sm = SessionManager(tmp_path)
    tool = _blocking_tool(sm)

    async def _force_fallback():
        for _ in range(100):
            await asyncio.sleep(0.01)
            if pa.is_waiting("cli:d"):
                assert pa.fallback("cli:d") is True
                return
        raise AssertionError("tool never registered a waiter")

    fb = asyncio.create_task(_force_fallback())
    out = await tool.execute(question="Pick one")
    await fb

    assert "presented to the user" in out
    assert "STOP" in out
    pa.reset()


@pytest.mark.asyncio
async def test_non_blocking_flag_keeps_v1(tmp_path):
    sm = SessionManager(tmp_path)
    tool = AskUserQuestionTool(sessions=sm, blocking=False)
    tool.set_context(RequestContext(channel="cli", chat_id="d", session_key="cli:d", metadata={}))
    out = await tool.execute(question="Old style?")
    assert "presented to the user" in out
    assert "STOP" in out


@pytest.mark.asyncio
async def test_blocking_skipped_without_live_consumer(tmp_path):
    """No loop consumer (single-message mode) → nobody can resolve the
    wait — the tool must yield immediately instead of riding the timeout."""
    from durin.agent import pending_answers as pa

    pa.reset()  # consumer flag off
    sm = SessionManager(tmp_path)
    tool = AskUserQuestionTool(sessions=sm, blocking=True, answer_timeout_s=60)
    tool.set_context(RequestContext(channel="cli", chat_id="d", session_key="cli:d", metadata={}))
    out = await tool.execute(question="Anyone?")
    assert "presented to the user" in out
    assert "STOP" in out


@pytest.mark.asyncio
async def test_blocking_skipped_for_non_interactive_sessions(tmp_path):
    """cron/heartbeat sessions never get interactive replies — yield now."""
    from durin.agent import pending_answers as pa

    pa.reset()
    pa.set_consumer_active(True)
    sm = SessionManager(tmp_path)
    tool = AskUserQuestionTool(sessions=sm, blocking=True, answer_timeout_s=60)
    tool.set_context(RequestContext(
        channel="cli", chat_id="d", session_key="cron:job42", metadata={},
    ))
    out = await tool.execute(question="Anyone?")
    assert "presented to the user" in out
    assert "STOP" in out
    pa.reset()
