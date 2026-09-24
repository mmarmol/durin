"""Every websocket turn ends with exactly one ``_turn_end`` carrying its outcome."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.providers.base import LLMResponse


def _loop(tmp_path: Path) -> tuple[AgentLoop, MessageBus]:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="Done", tool_calls=[]))
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    return loop, bus


def _msg() -> InboundMessage:
    return InboundMessage(
        channel="websocket", sender_id="u1", chat_id="chat1", content="hi",
        metadata={"client_msg_id": "cm-1"},
    )


async def _drain(bus: MessageBus) -> list:
    out = []
    while bus.outbound_size > 0:
        out.append(await bus.consume_outbound())
    return out


def _turn_ends(outbound: list) -> list:
    return [m for m in outbound if m.metadata.get("_turn_end")]


@pytest.mark.asyncio
async def test_completed_turn_end_carries_outcome_and_client_msg_id(tmp_path: Path) -> None:
    loop, bus = _loop(tmp_path)
    await loop._dispatch(_msg())
    ends = _turn_ends(await _drain(bus))
    assert len(ends) == 1
    assert ends[0].metadata["outcome"] == "completed"
    assert ends[0].metadata["client_msg_id"] == "cm-1"


@pytest.mark.asyncio
async def test_stopped_turn_publishes_stopped_turn_end(tmp_path: Path) -> None:
    loop, bus = _loop(tmp_path)
    started = asyncio.Event()

    async def _hang(*_a, **_kw):
        started.set()
        await asyncio.Event().wait()

    loop._process_message = _hang  # type: ignore[method-assign]
    task = asyncio.create_task(loop._dispatch(_msg()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ends = _turn_ends(await _drain(bus))
    assert [e.metadata["outcome"] for e in ends] == ["stopped"]


@pytest.mark.asyncio
async def test_failed_turn_publishes_failed_turn_end(tmp_path: Path) -> None:
    loop, bus = _loop(tmp_path)
    loop._process_message = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    await loop._dispatch(_msg())
    outbound = await _drain(bus)
    assert any(m.content == "Sorry, I encountered an error." for m in outbound)
    assert [e.metadata["outcome"] for e in _turn_ends(outbound)] == ["failed"]


@pytest.mark.asyncio
async def test_busy_lease_publishes_failed_turn_end(tmp_path: Path, monkeypatch) -> None:
    loop, bus = _loop(tmp_path)

    class _Busy:
        async def __aenter__(self):
            raise TimeoutError

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("durin.agent.loop.session_turn_lease", lambda _path: _Busy())
    await loop._dispatch(_msg())
    assert [e.metadata["outcome"] for e in _turn_ends(await _drain(bus))] == ["failed"]


@pytest.mark.asyncio
async def test_non_websocket_turns_publish_no_turn_end(tmp_path: Path) -> None:
    loop, bus = _loop(tmp_path)
    await loop._dispatch(InboundMessage(
        channel="telegram", sender_id="u1", chat_id="t1", content="hi",
    ))
    assert _turn_ends(await _drain(bus)) == []
