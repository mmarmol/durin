"""A turn waiting on the user's answer in a webui chat stops waiting once
nobody has watched that chat for a grace window. A viewer that comes back
inside the window (a page refresh, a network blip) keeps the wait alive."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent import pending_answers
from durin.channels.websocket import WebSocketChannel


@pytest.fixture(autouse=True)
def _clean_waiters():
    pending_answers.reset()
    yield
    pending_answers.reset()


def _channel(grace_s: float) -> WebSocketChannel:
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())
    channel._answer_grace_s = grace_s
    return channel


@pytest.mark.asyncio
async def test_last_viewer_leaving_releases_the_waiting_turn_after_the_grace() -> None:
    channel = _channel(0.02)
    conn = AsyncMock()
    channel._attach(conn, "c1")
    fut = pending_answers.create("websocket:c1")

    channel._cleanup_connection(conn)
    assert not fut.done()  # still inside the grace window

    # Wait for the release itself, not a fixed sleep, so a slow runner
    # cannot fail it; the ceiling only guards against a release that never comes.
    async with asyncio.timeout(5):
        assert await fut is pending_answers.FALLBACK


@pytest.mark.asyncio
async def test_a_viewer_back_inside_the_grace_keeps_the_turn_waiting() -> None:
    channel = _channel(0.05)
    first = AsyncMock()
    channel._attach(first, "c1")
    fut = pending_answers.create("websocket:c1")

    channel._cleanup_connection(first)
    channel._attach(AsyncMock(), "c1")  # the refreshed page re-subscribes
    await asyncio.sleep(0.1)

    assert not fut.done()
    assert pending_answers.resolve("websocket:c1", "green") is True
    assert await fut == "green"


@pytest.mark.asyncio
async def test_a_second_viewer_still_watching_keeps_the_turn_waiting() -> None:
    channel = _channel(0.02)
    tab_a, tab_b = AsyncMock(), AsyncMock()
    channel._attach(tab_a, "c1")
    channel._attach(tab_b, "c1")
    fut = pending_answers.create("websocket:c1")

    channel._cleanup_connection(tab_a)
    await asyncio.sleep(0.06)

    assert not fut.done()
    fut.cancel()


# An API client following the chat over SSE can answer a question with a plain
# message, but never an approval: for an approval only a webui tab (a socket
# connection) is a viewer.


def _sse():
    from durin.api.chat_stream import SseSubscriber

    return SseSubscriber()


@pytest.mark.asyncio
async def test_an_sse_viewer_does_not_hold_an_approval_after_the_last_tab_leaves() -> None:
    channel = _channel(0.02)
    tab, sse = AsyncMock(), _sse()
    channel._attach(tab, "c1")
    channel._attach(sse, "c1")
    fut = pending_answers.create("websocket:c1", kind="approval", ref="r1")

    channel._cleanup_connection(tab)
    assert not fut.done()  # still inside the grace window

    async with asyncio.timeout(5):
        assert await fut is pending_answers.FALLBACK
    assert sse in channel._subs["c1"]  # the API client keeps following


@pytest.mark.asyncio
async def test_an_sse_viewer_still_holds_a_question_after_the_last_tab_leaves() -> None:
    channel = _channel(0.02)
    tab, sse = AsyncMock(), _sse()
    channel._attach(tab, "c1")
    channel._attach(sse, "c1")
    fut = pending_answers.create("websocket:c1")

    channel._cleanup_connection(tab)
    await asyncio.sleep(0.1)

    assert not fut.done()
    assert pending_answers.resolve("websocket:c1", "green") is True
    assert await fut == "green"


@pytest.mark.asyncio
async def test_an_sse_attach_inside_the_grace_does_not_keep_an_approval_waiting() -> None:
    channel = _channel(0.05)
    tab = AsyncMock()
    channel._attach(tab, "c1")
    fut = pending_answers.create("websocket:c1", kind="approval", ref="r1")

    channel._cleanup_connection(tab)
    channel._attach(_sse(), "c1")  # an API client starts following the chat

    async with asyncio.timeout(5):
        assert await fut is pending_answers.FALLBACK


@pytest.mark.asyncio
async def test_a_question_turned_approval_inside_the_grace_is_released_without_a_tab() -> None:
    # The release checks what is waiting when it fires, not when it was set.
    channel = _channel(0.05)
    tab, sse = AsyncMock(), _sse()
    channel._attach(tab, "c1")
    channel._attach(sse, "c1")
    question = pending_answers.create("websocket:c1")

    channel._cleanup_connection(tab)
    assert pending_answers.resolve("websocket:c1", "green") is True
    assert await question == "green"
    approval_wait = pending_answers.create("websocket:c1", kind="approval", ref="r1")

    async with asyncio.timeout(5):
        assert await approval_wait is pending_answers.FALLBACK
