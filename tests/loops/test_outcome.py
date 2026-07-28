from durin.loops.outcome import build_outcome


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
