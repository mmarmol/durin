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


def test_interrupted_summary_carries_the_workflow_run_id_when_work_had_started():
    """The operator's handle for retaking partial work — only offered when
    sweep_orphans found the workflow's own manifest already on disk."""
    out = build_outcome("nightly", _record(status="interrupted", workflow_run_id="wf9",
                                           work_started=True))
    assert "wf9" in out.summary
    assert "may hold partial work" in out.summary


def test_interrupted_summary_omits_partial_work_claim_when_nothing_started():
    """workflow_run_id is minted and persisted before execute() ever runs (see
    LoopsRuntime._run), so it is present even for a run killed before the
    workflow took a single step. The summary must not hedge that unstarted
    work 'may' exist — it must say nothing happened, not contradict itself."""
    out = build_outcome("nightly", _record(status="interrupted", workflow_run_id="wf9",
                                           work_started=False))
    assert "may hold partial work" not in out.summary
    assert "wf9" not in out.summary


def test_origin_rides_along_untouched():
    origin = {"channel": "slack", "chat_id": "C1", "thread": "t1"}
    out = build_outcome("nightly", _record(origin=origin))
    assert out.origin == origin


def _out(status="no_goal", origin=None):
    return build_outcome("nightly", _record(status=status, origin=origin))


def test_a_session_origin_wins_over_a_declared_channel():
    """Asked in a conversation, answered in that conversation.

    A real session origin also carries a `channel` (and `chat_id`), so the
    fixture must too — otherwise this test can't tell a correctly-ordered
    session-then-channel check from a channel-first bug that happens to
    match on `channel` before ever looking at `kind`/`session_key`.
    """
    origin = {"kind": "session", "session_key": "websocket:abc", "channel": "slack", "chat_id": "C1"}
    dest = route(_out(origin=origin), operator_channel="slack")
    assert dest.kind == "session"
    assert dest.origin == origin


@pytest.mark.parametrize("status", ["done", "no_goal", "error", "escalated", "interrupted"])
def test_a_channel_origin_never_becomes_a_thread_destination(status):
    """The counterpart must never be handed an outcome, at any status.

    A channel origin names the external party the loop is corresponding with
    — a customer on a mail thread, say — not somebody who asked for this run:
    an inbound event fired it, nobody on durin's side did. An outcome is
    internal status (a raw exception string rides along in `detail`), so
    posting it into that thread would answer the customer with durin's own
    plumbing. Only workflow-authored prose goes down the counterpart lane.
    """
    origin = {"channel": "email", "sender": "customer@example.com", "chat_id": "C1",
              "thread": "t1", "subject": "invoice help", "reply": {}}
    dest = route(_out(status=status, origin=origin), operator_channel="slack")
    assert dest is None or dest.kind != "thread"


def test_a_channel_origin_reaches_the_operator_backstop():
    """Not delivering to the counterpart must not mean not delivering at all:
    a channel-fired run that ends badly is exactly what the operator backstop
    exists for, same as a cron fire nobody can reply to."""
    origin = {"channel": "email", "sender": "customer@example.com", "chat_id": "C1",
              "thread": "t1", "subject": "invoice help", "reply": {}}
    dest = route(_out(status="error", origin=origin), operator_channel="slack")
    assert dest.kind == "operator"
    assert dest.origin is None


def test_a_channel_origin_with_no_operator_channel_is_undeliverable():
    """With no backstop configured the outcome goes nowhere and the caller
    logs it — it must NOT fall back to the counterpart's thread as a
    consolation delivery."""
    origin = {"channel": "email", "sender": "customer@example.com", "chat_id": "C1",
              "thread": "t1", "subject": "invoice help", "reply": {}}
    assert route(_out(status="error", origin=origin), operator_channel=None) is None


def test_no_origin_falls_back_to_the_declared_channel():
    dest = route(_out(origin=None), operator_channel="slack")
    assert dest.kind == "operator"


def test_no_origin_and_no_declared_channel_is_undeliverable():
    assert route(_out(origin=None), operator_channel=None) is None


def test_done_is_suppressed_for_a_standing_destination():
    """Nobody asked; a scheduled loop meeting its goal is not news."""
    assert route(_out(status="done", origin=None), operator_channel="slack") is None


def test_done_is_delivered_when_somebody_asked():
    origin = {"kind": "session", "session_key": "websocket:abc", "channel": "slack", "chat_id": "C1"}
    dest = route(_out(status="done", origin=origin), operator_channel=None)
    assert dest.kind == "session"


@pytest.mark.parametrize("status", ["no_goal", "error", "escalated", "interrupted"])
def test_actionable_statuses_reach_a_standing_destination(status):
    assert route(_out(status=status, origin=None), operator_channel="slack") is not None


def test_a_webhook_origin_falls_back_to_the_operator_channel():
    """A webhook origin has no reply contract at all (channel_meta.build_reply
    has no webhook case), so the operator backstop is the only way this
    outcome reaches anybody. Kept alongside the channel-origin cases because
    it is the one origin shape whose `chat_id` is a hook name rather than a
    conversation — a routing rule that keyed on `chat_id` would regress here
    first."""
    origin = {"channel": "webhook", "chat_id": "my-hook", "thread": None, "subject": "my-hook", "reply": {}}
    dest = route(_out(origin=origin), operator_channel="slack")
    assert dest.kind == "operator"
