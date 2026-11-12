"""A workflow run's result is delivered to the conversation that launched it."""
from __future__ import annotations

import pytest

from durin.workflow import delivery

LAUNCH = {"kind": "session", "session_key": "websocket:chatA", "channel": "websocket",
          "chat_id": "chatA"}
_TAIL = ("Summarize the outcome for the user, and follow what the summary says about "
         "continuing the run.")


def test_the_message_is_addressed_to_the_launch_conversation():
    msg = delivery.result_message(LAUNCH, name="qa", run_id="r1", summary="done",
                                  resumed_from="the dashboard or the API")
    assert (msg.channel, msg.chat_id, msg.session_key_override) == (
        "system", "websocket:chatA", "websocket:chatA")
    assert msg.sender_id == "workflow_background"
    assert msg.metadata == {"injected_event": "workflow_background_result", "workflow": "qa"}
    assert msg.content == (
        "[Workflow 'qa' run r1, started from this conversation, was resumed from the "
        f"dashboard or the API and finished]\n\ndone\n\n{_TAIL}")


def test_without_resumed_from_the_text_is_todays():
    msg = delivery.result_message(LAUNCH, name="qa", run_id="r1", summary="done")
    assert msg.content == f"[Background workflow 'qa' finished]\n\ndone\n\n{_TAIL}"


def test_a_unified_session_keeps_its_key_and_the_real_chat():
    launch = {"kind": "session", "session_key": "unified:default", "channel": "slack",
              "chat_id": "C1"}
    msg = delivery.result_message(launch, name="qa", run_id="r1", summary="done")
    assert (msg.chat_id, msg.session_key_override) == ("slack:C1", "unified:default")


@pytest.mark.parametrize("launch", [
    {"kind": "automation", "name": "nightly"},
    {"kind": "service", "subject": "tok"},
    {},
    None,
    {"kind": "session", "session_key": "", "channel": "websocket", "chat_id": "chatA"},
    {"kind": "session", "channel": "websocket", "chat_id": "chatA"},
])
def test_a_launch_that_is_not_a_session_gives_no_message(launch):
    assert delivery.result_message(launch, name="qa", run_id="r1", summary="done") is None


class _Bus:
    def __init__(self, fail: bool = False):
        self.inbound = []
        self._fail = fail

    async def publish_inbound(self, msg):
        if self._fail:
            raise RuntimeError("bus closed")
        self.inbound.append(msg)


@pytest.mark.asyncio
async def test_deliver_publishes_one_message():
    bus = _Bus()
    assert await delivery.deliver(bus, LAUNCH, name="qa", run_id="r1", summary="done") is True
    assert [m.session_key_override for m in bus.inbound] == ["websocket:chatA"]


@pytest.mark.asyncio
async def test_deliver_never_raises():
    assert await delivery.deliver(None, LAUNCH, name="qa", run_id="r1", summary="done") is False
    assert await delivery.deliver(_Bus(fail=True), LAUNCH, name="qa", run_id="r1",
                                  summary="done") is False
    bus = _Bus()
    assert await delivery.deliver(bus, {"kind": "automation", "name": "n"}, name="qa",
                                  run_id="r1", summary="done") is False
    assert bus.inbound == []
