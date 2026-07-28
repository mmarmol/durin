import pytest

from durin.loops.outcome import build_outcome, route


def _record(**over):
    base = {
        "run_id": "r1", "status": "no_goal", "goal_reached": False,
        "origin": None, "workflow_run_id": "wf1", "ask": None, "detail": None,
    }
    return base | over


def test_summary_names_the_loop_the_run_and_the_status():
    out = build_outcome("nightly", _record())
    assert out.loop == "nightly"
    assert out.run_id == "r1"
    assert out.status == "no_goal"
    assert "nightly" in out.summary and "no_goal" in out.summary


def test_error_summary_carries_the_detail():
    out = build_outcome("nightly", _record(status="error", detail="provider timeout"))
    assert "provider timeout" in out.summary


def test_interrupted_summary_carries_the_workflow_run_id():
    """The operator's handle for retaking partial work."""
    out = build_outcome("nightly", _record(status="interrupted", workflow_run_id="wf9"))
    assert "wf9" in out.summary


def test_origin_rides_along_untouched():
    origin = {"channel": "slack", "chat_id": "C1", "thread": "t1"}
    out = build_outcome("nightly", _record(origin=origin))
    assert out.origin == origin


def _out(status="no_goal", origin=None):
    return build_outcome("nightly", _record(status=status, origin=origin))


def test_a_session_origin_wins_over_a_declared_channel():
    """Asked in a conversation, answered in that conversation."""
    origin = {"kind": "session", "session_key": "websocket:abc"}
    dest = route(_out(origin=origin), operator_channel="slack")
    assert dest.kind == "session"
    assert dest.origin == origin


def test_a_thread_origin_wins_over_a_declared_channel():
    origin = {"channel": "slack", "chat_id": "C1", "thread": "t1"}
    dest = route(_out(origin=origin), operator_channel="slack")
    assert dest.kind == "thread"


def test_no_origin_falls_back_to_the_declared_channel():
    dest = route(_out(origin=None), operator_channel="slack")
    assert dest.kind == "operator"


def test_no_origin_and_no_declared_channel_is_undeliverable():
    assert route(_out(origin=None), operator_channel=None) is None


def test_done_is_suppressed_for_a_standing_destination():
    """Nobody asked; a scheduled loop meeting its goal is not news."""
    assert route(_out(status="done", origin=None), operator_channel="slack") is None


def test_done_is_delivered_when_somebody_asked():
    origin = {"kind": "session", "session_key": "websocket:abc"}
    dest = route(_out(status="done", origin=origin), operator_channel=None)
    assert dest.kind == "session"


@pytest.mark.parametrize("status", ["no_goal", "error", "escalated", "interrupted"])
def test_actionable_statuses_reach_a_standing_destination(status):
    assert route(_out(status=status, origin=None), operator_channel="slack") is not None
