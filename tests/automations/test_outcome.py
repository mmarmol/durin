from durin.automations.outcome import ACTIONABLE_STATUSES, build_outcome, route
from durin.automations.spec import Delivery, Help


def _record(**over):
    base = {
        "run_id": "r1", "status": "failed", "origin": None,
        "workflow_run_id": "wf1", "detail": None, "final_route_label": None,
    }
    return base | over


def test_summary_names_the_automation_the_run_and_the_status():
    out = build_outcome("nightly", _record())
    assert out.automation == "nightly"
    assert out.run_id == "r1"
    assert out.status == "failed"
    assert "nightly" in out.summary and "failed" in out.summary


def test_summary_carries_the_detail():
    out = build_outcome("nightly", _record(detail="provider timeout"))
    assert "provider timeout" in out.summary


def test_origin_rides_along_untouched():
    origin = {"channel": "slack", "chat_id": "C1", "thread": "t1"}
    out = build_outcome("nightly", _record(origin=origin))
    assert out.origin == origin


def test_non_dict_origin_is_dropped():
    out = build_outcome("nightly", _record(origin="not-a-dict"))
    assert out.origin is None


def test_final_route_label_rides_along():
    out = build_outcome("nightly", _record(final_route_label="NOTHING_TO_REPORT"))
    assert out.final_route_label == "NOTHING_TO_REPORT"


def test_actionable_statuses_are_failed_and_interrupted_only():
    assert set(ACTIONABLE_STATUSES) == {"failed", "interrupted"}


def _out(status="failed", origin=None, **over):
    return build_outcome("nightly", _record(status=status, origin=origin, **over))


def test_a_session_origin_wins_over_delivery_and_help():
    origin = {"kind": "session", "session_key": "websocket:abc", "channel": "slack", "chat_id": "C1"}
    dest = route(_out(status="completed", origin=origin), deliver=False,
                 delivery=Delivery(channel="email", to="ops@x.com"), help=Help(channel="slack", to="C2"))
    assert dest.kind == "session"
    assert dest.origin == origin


def test_session_origin_wins_even_when_status_is_not_actionable_and_deliver_is_false():
    """Somebody asked — that always wins, independent of delivery policy."""
    origin = {"kind": "session", "session_key": "websocket:abc"}
    dest = route(_out(status="completed", origin=origin), deliver=False,
                 delivery=Delivery(), help=Help())
    assert dest.kind == "session"


def test_deliver_true_routes_to_the_delivery_channel():
    dest = route(_out(status="completed", origin=None), deliver=True,
                 delivery=Delivery(channel="email", to="ops@x.com"), help=Help())
    assert dest.kind == "delivery"
    assert dest.channel == "email"
    assert dest.to == "ops@x.com"


def test_deliver_true_but_no_delivery_channel_configured_falls_through():
    """deliver=True alone doesn't invent a destination — a channel must be configured."""
    dest = route(_out(status="completed", origin=None), deliver=True,
                 delivery=Delivery(channel=None), help=Help())
    assert dest is None


def test_actionable_status_reaches_the_help_backstop_when_delivery_is_silent():
    dest = route(_out(status="failed", origin=None), deliver=False,
                 delivery=Delivery(channel=None), help=Help(channel="slack", to="ops"))
    assert dest.kind == "help"
    assert dest.channel == "slack"
    assert dest.to == "ops"


def test_non_actionable_status_never_reaches_the_help_backstop():
    dest = route(_out(status="completed", origin=None), deliver=False,
                 delivery=Delivery(channel=None), help=Help(channel="slack", to="ops"))
    assert dest is None


def test_actionable_status_with_no_help_channel_configured_is_undeliverable():
    dest = route(_out(status="failed", origin=None), deliver=False,
                 delivery=Delivery(channel=None), help=Help(channel=None))
    assert dest is None


def test_delivery_destination_preferred_over_help_backstop_when_both_apply():
    dest = route(_out(status="failed", origin=None), deliver=True,
                 delivery=Delivery(channel="email", to="ops@x.com"), help=Help(channel="slack", to="C2"))
    assert dest.kind == "delivery"


# ---------------------------------------------------------------------------
# Fix round 1, finding 3: AutomationOutcome carries the routed destination
# (populated by AutomationsRuntime._deliver_outcome, not build_outcome).
# ---------------------------------------------------------------------------

def test_build_outcome_leaves_destination_fields_unset():
    out = build_outcome("nightly", _record())
    assert out.kind is None
    assert out.channel is None
    assert out.to is None


# ---------------------------------------------------------------------------
# Fix round 1, finding 5: achieved on a help-only automation must be heard —
# achieving is the counterpart of escalating.
# ---------------------------------------------------------------------------

def test_achieved_with_only_help_channel_routes_to_help():
    dest = route(_out(status="achieved", origin=None), deliver=True,
                 delivery=Delivery(channel=None), help=Help(channel="slack", to="ops-room"))
    assert dest.kind == "help"
    assert dest.channel == "slack" and dest.to == "ops-room"


def test_achieved_with_neither_channel_configured_is_still_undeliverable():
    """Never invents a destination — even for achieved."""
    dest = route(_out(status="achieved", origin=None), deliver=True,
                 delivery=Delivery(channel=None), help=Help(channel=None))
    assert dest is None


def test_achieved_prefers_the_delivery_channel_over_help_when_both_configured():
    dest = route(_out(status="achieved", origin=None), deliver=True,
                 delivery=Delivery(channel="email", to="ops@x.com"), help=Help(channel="slack", to="C2"))
    assert dest.kind == "delivery"
    assert dest.channel == "email"


def test_achieved_help_backstop_still_yields_to_a_session_origin():
    origin = {"kind": "session", "session_key": "websocket:abc"}
    dest = route(_out(status="achieved", origin=origin), deliver=True,
                 delivery=Delivery(channel=None), help=Help(channel="slack", to="ops-room"))
    assert dest.kind == "session"
