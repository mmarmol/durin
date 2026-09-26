"""A request decided outside the turn that asked tells that chat how it ended.

The turn was told the request is waiting and moved on, so a later decision
(the Pending page, a click that lands after the wait) posts a system note into
the chat session that asked — the way a background workflow's result is
delivered. A verdict handed to a turn still waiting needs no note: that turn
reports it itself. A context with no person, or a chat this process does not
serve, gets none either.
"""

from __future__ import annotations

import pytest

from durin.agent.approval import Outcome
from durin.agent.approval_notify import chat_route, notify_origin, origin_note
from durin.bus.events import InboundMessage


def _record(status: str, *, session: str | None = "websocket:abc", **extra) -> dict:
    return {
        "id": "a1b2c3d4e5f6",
        "kind": "skill_install",
        "summary": "install skill 'mailer' from github:acme/mailer",
        "requested_by_session": session,
        "status": status,
        **extra,
    }


def _serves_all(_channel: str) -> bool:
    return True


def test_an_applied_request_posts_an_approved_note_into_the_chat() -> None:
    outcome = Outcome("applied", _record("applied"), {"installed": "mailer"}, "Done.")

    note = origin_note(outcome, serves=_serves_all)

    assert isinstance(note, InboundMessage)
    assert note.channel == "system"
    assert note.session_key_override == "websocket:abc"
    assert note.chat_id == "websocket:abc"
    assert note.metadata["injected_event"] == "approval_decision"
    assert note.metadata["approval_id"] == "a1b2c3d4e5f6"
    assert ("Approved: install skill 'mailer' from github:acme/mailer — result: "
            '{"installed": "mailer"}') in note.content


def test_a_rejected_request_posts_a_rejected_note() -> None:
    outcome = Outcome("rejected", _record("rejected"), None, "Declined.")

    note = origin_note(outcome, serves=_serves_all)

    assert note is not None
    assert "Rejected: install skill 'mailer' from github:acme/mailer" in note.content
    assert "Approved:" not in note.content
    assert "do not reach the same effect another way" in note.content


def test_an_approved_request_whose_run_failed_reports_the_failure() -> None:
    outcome = Outcome("failed", _record("failed", result={"error": "boom"}), None, "failed")

    note = origin_note(outcome, serves=_serves_all)

    assert note is not None
    assert "Approved: install skill 'mailer' from github:acme/mailer — result: failed: boom" \
        in note.content


def test_an_approved_request_whose_target_changed_reports_it_was_not_run() -> None:
    outcome = Outcome("stale", _record("stale"), None, "Not run.")

    note = origin_note(outcome, serves=_serves_all)

    assert note is not None
    assert "Approved: install skill 'mailer' from github:acme/mailer — result: not run" \
        in note.content


def test_an_expired_request_was_not_decided_so_no_note() -> None:
    outcome = Outcome("stale", _record("expired"), None, "expired")

    assert origin_note(outcome, serves=_serves_all) is None


def test_a_verdict_handed_to_the_waiting_turn_gets_no_note() -> None:
    outcome = Outcome("pending", _record("pending"), None, "Handed to the waiting turn.")

    assert origin_note(outcome, serves=_serves_all) is None


def test_a_refused_decision_gets_no_note() -> None:
    outcome = Outcome("refused", _record("pending"), None, "cannot run here")

    assert origin_note(outcome, serves=_serves_all) is None


@pytest.mark.parametrize("session", ["cron:nightly", "workflow:r1:root", None])
def test_a_request_from_a_context_with_no_person_gets_no_note(session) -> None:
    outcome = Outcome("applied", _record("applied", session=session), {}, "Done.")

    assert origin_note(outcome, serves=_serves_all) is None


def test_a_chat_this_process_does_not_serve_gets_no_note() -> None:
    # A TUI session (cli:) lives in another process; the gateway must not run
    # a turn in it.
    outcome = Outcome("applied", _record("applied", session="cli:direct"), {}, "Done.")

    assert origin_note(outcome, serves=lambda channel: channel != "cli") is None


@pytest.mark.parametrize(("key", "route"), [
    ("websocket:3f2a", ("websocket", "3f2a")),
    ("slack:C123", ("slack", "C123")),
    ("slack:C123:1712345678.000100", ("slack", "C123")),
    ("email:ana@example.com:9f8e7d", ("email", "ana@example.com")),
    ("feishu:oc_1:om_2", ("feishu", "oc_1")),
    ("discord:111:thread:222", ("discord", "222")),
    ("discord:333", ("discord", "333")),
    ("telegram:-100:topic:7", ("telegram", "-100")),
    ("telegram:42", ("telegram", "42")),
    ("matrix:!room:example.org", ("matrix", "!room:example.org")),
    ("unified:default", None),
    ("nocolon", None),
])
def test_chat_route_maps_a_session_key_to_where_its_replies_go(key, route) -> None:
    assert chat_route(key) == route


class _Bus:
    def __init__(self) -> None:
        self.inbound: list[InboundMessage] = []

    async def publish_inbound(self, msg: InboundMessage) -> None:
        self.inbound.append(msg)


@pytest.mark.asyncio
async def test_notify_origin_publishes_the_note_on_the_bus() -> None:
    bus = _Bus()
    outcome = Outcome("rejected", _record("rejected"), None, "Declined.")

    assert await notify_origin(bus, outcome, serves=_serves_all) is True
    assert len(bus.inbound) == 1 and bus.inbound[0].channel == "system"


@pytest.mark.asyncio
async def test_notify_origin_publishes_nothing_for_a_hand_off() -> None:
    bus = _Bus()
    outcome = Outcome("pending", _record("pending"), None, "Handed to the waiting turn.")

    assert await notify_origin(bus, outcome, serves=_serves_all) is False
    assert bus.inbound == []
