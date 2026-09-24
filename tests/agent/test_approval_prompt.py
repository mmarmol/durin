"""Tests for durin.agent.approval_prompt — the in-chat approval asker."""

import asyncio
from types import SimpleNamespace

import pytest

from durin.agent import pending_answers as pa
from durin.agent.approval_prompt import make_chat_asker
from durin.agent.user_payloads import PENDING_APPROVAL_KEY, pending_interaction_items


class _Sessions:
    def __init__(self):
        self.s = SimpleNamespace(metadata={})

    def get_or_create(self, key):
        return self.s

    def save(self, session, **kw):
        pass


class _Bus:
    def __init__(self):
        self.out = []

    async def publish_outbound(self, msg):
        self.out.append(msg)


REC = {"id": "r1", "kind": "exec_command", "summary": "run `rm -rf build`",
       "detail": {"command": "rm -rf build", "rule": r"\brm\s+-[rf]{1,2}\b"}}


@pytest.fixture(autouse=True)
def _consumer():
    pa.reset()
    pa.set_consumer_active(True)
    yield
    pa.reset()


def _ctx(channel):
    return SimpleNamespace(session_key=f"{channel}:c1", channel=channel, chat_id="c1")


@pytest.mark.asyncio
async def test_asker_waits_and_returns_the_verdict():
    sessions, bus = _Sessions(), _Bus()
    ask = make_chat_asker(sessions=sessions, bus=bus, request_ctx=_ctx("websocket"), timeout_s=5)
    task = asyncio.create_task(ask(REC))
    await asyncio.sleep(0)
    assert sessions.s.metadata[PENDING_APPROVAL_KEY]["approval_id"] == "r1"
    assert pa.waiting_kind("websocket:c1") == "approval"
    pa.resolve("websocket:c1", "approve")
    assert await task == "approve"
    assert PENDING_APPROVAL_KEY not in sessions.s.metadata
    assert bus.out == []  # rich channel: no text copy


@pytest.mark.asyncio
async def test_text_channel_gets_the_question_once_and_timeout_returns_none():
    sessions, bus = _Sessions(), _Bus()
    ask = make_chat_asker(sessions=sessions, bus=bus, request_ctx=_ctx("slack"), timeout_s=0.05)
    assert await ask(REC) is None
    assert len(bus.out) == 1
    assert "Approval needed" in bus.out[0].content and "yes" in bus.out[0].content
    # Delivered once: the turn-end fallback has nothing left to re-send.
    assert pending_interaction_items(sessions.s.metadata) == []


@pytest.mark.asyncio
async def test_text_channel_reask_after_timeout_sends_a_second_copy():
    """A timed-out approval that gets asked again must not be swallowed by
    the "already delivered" bookkeeping — the person needs to see it again."""
    sessions, bus = _Sessions(), _Bus()
    ask = make_chat_asker(sessions=sessions, bus=bus, request_ctx=_ctx("slack"), timeout_s=0.05)
    assert await ask(REC) is None
    assert await ask(REC) is None
    assert len(bus.out) == 2


def test_no_asker_without_a_person():
    pa.set_consumer_active(False)
    assert make_chat_asker(sessions=_Sessions(), bus=_Bus(),
                           request_ctx=_ctx("websocket"), timeout_s=5) is None


@pytest.mark.asyncio
async def test_timeout_racing_a_landed_resolve_still_returns_the_verdict(monkeypatch):
    """resolve() can land in the same loop iteration the timeout's own
    cancellation fires. Simulate that race directly: the future gets its
    result inside asyncio.timeout's __aenter__, then __aexit__ still raises
    TimeoutError, as the real deadline callback can when it wins a tie."""
    import durin.agent.approval_prompt as approval_prompt_mod

    class _RaceTimeout:
        def __init__(self, delay):
            pass

        async def __aenter__(self):
            pa.resolve("websocket:c1", "approve")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            raise asyncio.TimeoutError()

    monkeypatch.setattr(approval_prompt_mod.asyncio, "timeout", _RaceTimeout)
    sessions, bus = _Sessions(), _Bus()
    ask = make_chat_asker(sessions=sessions, bus=bus, request_ctx=_ctx("websocket"), timeout_s=0)
    assert await ask(REC) == "approve"
