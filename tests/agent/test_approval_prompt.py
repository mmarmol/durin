"""Tests for durin.agent.approval_prompt — the in-chat approval asker."""

import asyncio
from types import SimpleNamespace

import pytest

from durin.agent import pending_answers as pa
from durin.agent.approval_prompt import ChatHandles, make_chat_asker
from durin.agent.user_payloads import PENDING_APPROVAL_KEY, pending_interaction_items
from durin.bus.events import OUTBOUND_META_ASKS_PERSON


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


def _ctx(channel, metadata=None):
    ns = SimpleNamespace(session_key=f"{channel}:c1", channel=channel, chat_id="c1")
    if metadata is not None:
        ns.metadata = metadata
    return ns


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
    # A rich channel gets no text copy. It gets two state snapshots instead:
    # the card appears, then it clears once the verdict is in.
    assert [m.metadata.get("_goal_state_sync") for m in bus.out] == [True, True]
    assert all(m.content == "" for m in bus.out)
    assert bus.out[0].metadata["goal_state"]["pending_approval"]["approval_id"] == "r1"
    assert "pending_approval" not in bus.out[1].metadata["goal_state"]


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
async def test_text_channel_publishes_the_turns_metadata_and_the_asks_person_flag():
    """The published copy must carry the turn's own metadata (so the channel
    can place it in the right thread/topic) plus the flag that tells Slack to
    notify instead of silently editing a status line."""
    sessions, bus = _Sessions(), _Bus()
    turn_metadata = {"slack": {"thread_ts": "200.000"}, "message_thread_id": 7}
    ask = make_chat_asker(
        sessions=sessions, bus=bus,
        request_ctx=_ctx("slack", metadata=turn_metadata), timeout_s=0.05,
    )
    assert await ask(REC) is None
    assert len(bus.out) == 1
    sent_meta = bus.out[0].metadata
    assert sent_meta["slack"] == {"thread_ts": "200.000"}
    assert sent_meta["message_thread_id"] == 7
    assert sent_meta[OUTBOUND_META_ASKS_PERSON] is True
    # The context's own metadata dict must never be mutated or reused.
    assert sent_meta is not turn_metadata


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


def test_chat_handles_from_tool_context_reads_the_configured_timeout():
    sessions, bus = _Sessions(), _Bus()
    ctx = SimpleNamespace(
        sessions=sessions, bus=bus,
        app_config=SimpleNamespace(agents=SimpleNamespace(
            defaults=SimpleNamespace(ask_user_answer_timeout_s=42))))
    handles = ChatHandles.from_tool_context(ctx)
    assert handles.sessions is sessions and handles.bus is bus
    assert handles.timeout_s == 42.0


def test_chat_handles_from_tool_context_falls_back_to_300_without_config():
    ctx = SimpleNamespace(sessions=_Sessions(), bus=None)  # no app_config at all
    assert ChatHandles.from_tool_context(ctx).timeout_s == 300.0
