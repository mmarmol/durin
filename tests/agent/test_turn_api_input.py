"""The loop marks a turn that received input from an API token, so the turn
never asks the person in the chat to approve a privileged action (see
test_approval_api_input)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent import pending_answers
from durin.agent.approval_prompt import ChatHandles
from durin.agent.loop import AgentLoop, PendingQueues
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.providers.base import LLMResponse


@pytest.fixture(autouse=True)
def _consumer(monkeypatch):
    monkeypatch.setattr(pending_answers, "_CONSUMER_ACTIVE", True)


def _loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "m"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="m")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    return loop


class _Sessions:
    def get_or_create(self, key):
        return SimpleNamespace(metadata={})

    def save(self, session, **kw):
        pass


def _asks_in_chat() -> bool:
    """Whether a privileged tool running now would ask the person in chat c1."""
    ctx = SimpleNamespace(channel="websocket", chat_id="c1", session_key="websocket:c1")
    return ChatHandles(sessions=_Sessions(), timeout_s=5).asker(ctx) is not None


@pytest.mark.parametrize(("metadata", "asks"), [
    ({"webui": True}, True),
    ({"webui": True, "origin": "api"}, False),
])
@pytest.mark.asyncio
async def test_turn_opened_by_api_input_never_asks_in_the_chat(tmp_path: Path, metadata, asks) -> None:
    loop = _loop(tmp_path)
    seen = {}

    async def _turn(*_a, **_kw):
        seen["asks"] = _asks_in_chat()
        return None

    loop._process_message = _turn  # type: ignore[method-assign]
    await loop._dispatch(InboundMessage(
        channel="websocket", sender_id="u", chat_id="c1", content="hi", metadata=metadata,
    ))
    assert seen["asks"] is asks


@pytest.mark.asyncio
async def test_api_input_injected_into_a_person_s_turn_stops_asking_for_what_follows(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    queues = PendingQueues.create()
    await queues.deferred.put(InboundMessage(
        channel="websocket", sender_id="api:t", chat_id="c1", content="also do this",
        metadata={"origin": "api"},
    ))
    asks = []

    async def _chat(*_a, **_kw):
        asks.append(_asks_in_chat())
        return LLMResponse(content=f"reply {len(asks)}", tool_calls=[])

    loop.provider.chat_with_retry = AsyncMock(side_effect=_chat)
    await loop._run_agent_loop(
        [{"role": "user", "content": "hi"}], channel="websocket", chat_id="c1",
        session_key="websocket:c1", pending_queues=queues,
    )
    # The person's own step would ask them; after the API message joined, nothing is asked.
    assert asks == [True, False]


def _reply(content: str, *, api: bool, media: list[str] | None = None) -> InboundMessage:
    metadata = {"webui": True, "client_msg_id": "cm-1"}
    if api:
        metadata["origin"] = "api"
    return InboundMessage(
        channel="websocket", sender_id="api:t" if api else "u", chat_id="c1",
        content=content, media=media or [], metadata=metadata,
    )


@pytest.mark.asyncio
async def test_an_api_yes_never_answers_an_approval(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    waiter = pending_answers.create("websocket:c1", kind="approval", ref="r1")
    try:
        # Not consumed: it routes on as any other message during the turn,
        # and it is not acknowledged as the answer.
        assert await loop._answer_pending_question(_reply("yes", api=True), "websocket:c1") is False
        assert loop.bus.outbound_size == 0
        # Nor does a media reply make the approval stop waiting.
        assert loop._maybe_resolve_pending_answer(
            _reply("see", api=True, media=["/tmp/x.png"]), "websocket:c1") is False
        assert not waiter.done()
        assert pending_answers.waiting_kind("websocket:c1") == "approval"
        # The person's own "yes" still decides it.
        assert loop._maybe_resolve_pending_answer(_reply("yes", api=False), "websocket:c1") is True
        assert await waiter == "approve"
    finally:
        pending_answers.discard("websocket:c1", waiter)


@pytest.mark.asyncio
async def test_an_api_client_may_still_answer_a_question(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    waiter = pending_answers.create("websocket:c1")
    try:
        assert await loop._answer_pending_question(_reply("yes", api=True), "websocket:c1") is True
        assert await waiter == "yes"
    finally:
        pending_answers.discard("websocket:c1", waiter)


async def _question_then_gated_action(loop: AgentLoop, answer: InboundMessage) -> bool:
    """One webui turn: a tool asks the person a question and waits (in its own
    task, as the runner runs a batch of tools), the reply arrives through the
    loop, and a later privileged tool in the same turn checks whether it may
    ask in the chat. Returns that last check."""
    import asyncio

    from durin.agent.tools.ask_user import AskUserQuestionTool
    from durin.agent.tools.context import RequestContext

    seen = {}

    async def _turn(msg, **_kw):
        tool = AskUserQuestionTool(sessions=loop.sessions, bus=loop.bus, answer_timeout_s=5)
        tool.set_context(RequestContext(channel="websocket", chat_id="c1",
                                        session_key="websocket:c1"))
        [result] = await asyncio.gather(tool.execute(question="Which one?"))
        seen["answered"] = "answered" in result
        seen["asks"] = _asks_in_chat()
        return None

    loop._process_message = _turn  # type: ignore[method-assign]
    turn = asyncio.create_task(loop._dispatch(InboundMessage(
        channel="websocket", sender_id="u", chat_id="c1", content="start",
        metadata={"webui": True})))
    for _ in range(500):
        if pending_answers.is_waiting("websocket:c1"):
            break
        await asyncio.sleep(0.01)
    assert await loop._answer_pending_question(answer, "websocket:c1") is True
    await asyncio.wait_for(turn, 5)
    assert seen["answered"] is True
    return seen["asks"]


@pytest.mark.asyncio
async def test_an_api_answer_to_a_question_marks_the_turn_as_api_input(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    asks = await _question_then_gated_action(loop, _reply("the blue one", api=True))
    # The program that answered is now part of the turn: what follows is not
    # put to the person in the chat.
    assert asks is False


@pytest.mark.asyncio
async def test_a_person_s_answer_to_a_question_leaves_the_turn_a_person_s(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    asks = await _question_then_gated_action(loop, _reply("the blue one", api=False))
    assert asks is True
