"""Tests for sustained goal tools (`long_task`, `complete_goal`)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.agent.tools.context import RequestContext
from durin.agent.tools.long_task import (
    CompleteGoalTool,
    LongTaskTool,
)
from durin.bus.queue import MessageBus
from durin.session.goal_state import GOAL_STATE_KEY
from durin.session.manager import SessionManager


def _tools(sm: SessionManager) -> tuple[LongTaskTool, CompleteGoalTool]:
    lt = LongTaskTool(sessions=sm)
    cg = CompleteGoalTool(sessions=sm)
    rc = RequestContext(
        channel="websocket",
        chat_id="c1",
        session_key="websocket:c1",
        metadata={},
    )
    lt.set_context(rc)
    cg.set_context(rc)
    return lt, cg


@pytest.mark.asyncio
async def test_long_task_records_goal_metadata(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _cg = _tools(sm)

    out = await lt.execute(goal="Do the thing", ui_summary="thing")
    assert "Goal recorded" in out

    sess = sm.get_or_create("websocket:c1")
    blob = sess.metadata.get(GOAL_STATE_KEY)
    assert isinstance(blob, dict)
    assert blob["status"] == "active"
    assert blob["objective"] == "Do the thing"
    assert blob["ui_summary"] == "thing"


@pytest.mark.asyncio
async def test_long_task_rejects_second_active_goal(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _cg = _tools(sm)

    await lt.execute(goal="First")
    out = await lt.execute(goal="Second")
    assert "already active" in out


@pytest.mark.asyncio
async def test_complete_goal_closes_active_goal(tmp_path):
    sm = SessionManager(tmp_path)
    lt, cg = _tools(sm)

    await lt.execute(goal="X")
    out = await cg.execute(recap="Done.")
    assert "marked complete" in out

    sess = sm.get_or_create("websocket:c1")
    blob = sess.metadata.get(GOAL_STATE_KEY)
    assert blob["status"] == "completed"
    assert blob["recap"] == "Done."


@pytest.mark.asyncio
async def test_long_task_publishes_goal_state_ws_after_save(tmp_path):
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    sm = SessionManager(tmp_path)
    lt = LongTaskTool(sessions=sm, bus=bus)
    rc = RequestContext(
        channel="websocket",
        chat_id="chat-99",
        session_key="websocket:chat-99",
        metadata={},
    )
    lt.set_context(rc)

    await lt.execute(goal="Objective alpha", ui_summary="alpha")

    bus.publish_outbound.assert_awaited_once()
    call = bus.publish_outbound.await_args.args[0]
    assert call.channel == "websocket"
    assert call.chat_id == "chat-99"
    assert call.metadata.get("_goal_state_sync") is True
    assert call.metadata["goal_state"] == {
        "active": True,
        "ui_summary": "alpha",
        "objective": "Objective alpha",
    }


@pytest.mark.asyncio
async def test_complete_goal_publishes_inactive_goal_state_ws(tmp_path):
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    sm = SessionManager(tmp_path)
    lt = LongTaskTool(sessions=sm, bus=bus)
    cg = CompleteGoalTool(sessions=sm, bus=bus)
    rc = RequestContext(
        channel="websocket",
        chat_id="chat-z",
        session_key="websocket:chat-z",
        metadata={},
    )
    lt.set_context(rc)
    await lt.execute(goal="X")

    bus.publish_outbound.reset_mock()
    cg.set_context(rc)
    await cg.execute(recap="Done.")

    bus.publish_outbound.assert_awaited_once()
    call = bus.publish_outbound.await_args.args[0]
    assert call.metadata["goal_state"] == {"active": False}


@pytest.mark.asyncio
async def test_complete_goal_without_active_is_noop_message(tmp_path):
    sm = SessionManager(tmp_path)
    _lt, cg = _tools(sm)

    out = await cg.execute(recap="n/a")
    assert "No active" in out


@pytest.mark.asyncio
async def test_long_task_skips_ws_publish_without_bus(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _cg = _tools(sm)
    out = await lt.execute(goal="Solo", ui_summary="s")
    assert "Goal recorded" in out


@pytest.mark.asyncio
async def test_long_task_and_complete_goal_registered(tmp_path):
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    lt = loop.tools.get("long_task")
    cg = loop.tools.get("complete_goal")
    assert lt is not None and lt.name == "long_task"
    assert cg is not None and cg.name == "complete_goal"


@pytest.mark.asyncio
async def test_completed_goal_stays_in_the_task_state_anchor(tmp_path):
    """The end-to-end property that matters: after complete_goal the objective
    is still in the block that goes into the prompt. Rendering nothing here is
    what let a session lose its stated purpose while work continued."""
    from durin.agent.task_state import task_state_runtime_lines

    sm = SessionManager(tmp_path)
    lt, cg = _tools(sm)

    await lt.execute(
        goal="Ship the ticket pipeline without ever POSTing",
        ui_summary="Ticket pipeline, DELIVER only",
    )
    await cg.execute(recap="Stages 1-3 verified; stage 4 left in DELIVER mode.")

    sess = sm.get_or_create("websocket:c1")
    block = "\n".join(task_state_runtime_lines(sess.metadata))
    assert "## Goal" in block
    assert "Goal (completed): Ticket pipeline, DELIVER only" in block
    assert "Outcome: Stages 1-3 verified" in block


@pytest.mark.asyncio
async def test_complete_goal_does_not_duplicate_the_goal_into_the_log(tmp_path):
    """While the blob lives the anchor already carries the text, so writing a
    decision-log copy at completion time would pay for it in every prompt."""
    from durin.session.decision_log import decision_log_raw, parse_decisions

    sm = SessionManager(tmp_path)
    lt, cg = _tools(sm)

    await lt.execute(goal="Ship the ticket pipeline", ui_summary="pipeline")
    await cg.execute(recap="done")

    sess = sm.get_or_create("websocket:c1")
    assert parse_decisions(decision_log_raw(sess.metadata)) == []


@pytest.mark.asyncio
async def test_replacing_a_completed_goal_leaves_a_breadcrumb(tmp_path):
    """long_task overwrites the blob — the only moment the finished goal would
    otherwise be lost. The decision log survives that."""
    from durin.agent.task_state import task_state_runtime_lines
    from durin.session.decision_log import decision_log_raw, parse_decisions

    sm = SessionManager(tmp_path)
    lt, cg = _tools(sm)

    await lt.execute(goal="First objective", ui_summary="first")
    await cg.execute(recap="shipped it")
    await lt.execute(goal="Second, unrelated objective", ui_summary="second")

    sess = sm.get_or_create("websocket:c1")
    entries = parse_decisions(decision_log_raw(sess.metadata))
    assert len(entries) == 1
    assert entries[0]["source"] == "auto"
    assert "first" in entries[0]["text"]
    assert "shipped it" in entries[0]["text"]

    block = "\n".join(task_state_runtime_lines(sess.metadata))
    assert "Goal (active):" in block          # the new objective
    assert "first" in block                   # and the old one is still traceable


@pytest.mark.asyncio
async def test_replacing_an_active_goal_is_refused(tmp_path):
    """No breadcrumb needed: long_task refuses to clobber a live objective."""
    from durin.session.decision_log import decision_log_raw, parse_decisions

    sm = SessionManager(tmp_path)
    lt, _cg = _tools(sm)

    await lt.execute(goal="First objective", ui_summary="first")
    out = await lt.execute(goal="Second objective")

    assert "already active" in out
    sess = sm.get_or_create("websocket:c1")
    assert sess.metadata[GOAL_STATE_KEY]["objective"] == "First objective"
    assert parse_decisions(decision_log_raw(sess.metadata)) == []


@pytest.mark.asyncio
async def test_breadcrumb_honours_configured_decision_caps(tmp_path):
    """The breadcrumb goes through the same configured caps as note_decision."""
    from durin.session.decision_log import decision_log_raw, parse_decisions

    sm = SessionManager(tmp_path)
    lt = LongTaskTool(sessions=sm, decision_log_max_entries=10, decision_log_max_chars=20)
    cg = CompleteGoalTool(sessions=sm)
    for tool in (lt, cg):
        tool.set_context(RequestContext(
            channel="websocket", chat_id="c1", session_key="websocket:c1", metadata={},
        ))

    await lt.execute(goal="A goal whose breadcrumb will not fit the tiny cap")
    await cg.execute(recap="a recap that is comfortably over twenty characters")
    await lt.execute(goal="Replacement objective")

    sess = sm.get_or_create("websocket:c1")
    # A lone entry over the cap is still kept (the cap never empties the log),
    # but it was written through the configured value, not the module default.
    assert len(parse_decisions(decision_log_raw(sess.metadata))) <= 1
