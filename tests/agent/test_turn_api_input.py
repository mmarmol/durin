"""The loop marks a turn that received input from an API token, so the
approval gate stages privileged actions in it (see test_approval_api_input)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent import approval, pending_answers
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


def _privileged(tmp_path: Path) -> approval.Decision:
    return approval.gate(tmp_path, "mcp", action="install", summary="x", session_key="websocket:c1")


@pytest.mark.parametrize(("metadata", "allowed"), [
    ({"webui": True}, True),
    ({"webui": True, "origin": "api"}, False),
])
@pytest.mark.asyncio
async def test_turn_opened_by_api_input_stages_privileged_actions(tmp_path: Path, metadata, allowed) -> None:
    loop = _loop(tmp_path)
    seen = {}

    async def _turn(*_a, **_kw):
        seen["decision"] = _privileged(tmp_path)
        return None

    loop._process_message = _turn  # type: ignore[method-assign]
    await loop._dispatch(InboundMessage(
        channel="websocket", sender_id="u", chat_id="c1", content="hi", metadata=metadata,
    ))
    assert seen["decision"].allow is allowed


@pytest.mark.asyncio
async def test_api_input_injected_into_a_person_s_turn_stages_what_follows(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    queues = PendingQueues.create()
    await queues.deferred.put(InboundMessage(
        channel="websocket", sender_id="api:t", chat_id="c1", content="also do this",
        metadata={"origin": "api"},
    ))
    decisions = []

    async def _chat(*_a, **_kw):
        decisions.append(_privileged(tmp_path).allow)
        return LLMResponse(content=f"reply {len(decisions)}", tool_calls=[])

    loop.provider.chat_with_retry = AsyncMock(side_effect=_chat)
    await loop._run_agent_loop(
        [{"role": "user", "content": "hi"}], channel="websocket", chat_id="c1",
        session_key="websocket:c1", pending_queues=queues,
    )
    # The person's own step is allowed; after the API message joined, it is not.
    assert decisions == [True, False]
