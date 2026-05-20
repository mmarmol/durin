"""D3 editor-advanced tests: multi-line input (D3.1) + shell paste (D3.2)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.cli.tui.app import DurinApp
from durin.cli.tui.widgets import ChatView, InputArea, MessageBubble


def _fake_agent_loop(bus: MessageBus, tmp_path) -> SimpleNamespace:
    async def _idle_run() -> None:
        await asyncio.Event().wait()

    return SimpleNamespace(
        bus=bus,
        workspace=str(tmp_path),
        model="m",
        model_preset="default",
        context_window_tokens=200_000,
        sessions=SimpleNamespace(
            get_or_create=lambda key: SimpleNamespace(messages=[], metadata={})
        ),
        run=_idle_run,
    )


async def _drain(bus: MessageBus, *, timeout: float = 0.5) -> list[InboundMessage]:
    received: list[InboundMessage] = []
    while True:
        try:
            msg = await asyncio.wait_for(bus.consume_inbound(), timeout=timeout)
        except asyncio.TimeoutError:
            return received
        received.append(msg)


# ---------------------------------------------------------------------------
# D3.1 — multi-line input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alt_enter_inserts_newline(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "first line"
        # Move cursor to end of input
        inp.cursor_position = len(inp.value)
        await pilot.press("alt+enter")
        await pilot.pause()
        assert inp.value == "first line\n"


@pytest.mark.asyncio
async def test_multi_line_value_publishes_with_newlines_preserved(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        # Manually set a multi-line value (as if the user used Alt+Enter
        # several times or pasted multi-line text).
        inp.value = "line one\nline two\nline three"
        await pilot.press("enter")
        await pilot.pause()
        received = await _drain(bus)

    assert received, "expected the multi-line value to publish"
    assert received[0].content == "line one\nline two\nline three"


# ---------------------------------------------------------------------------
# D3.2 — shell paste integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_bang_publishes_command_output(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "!echo hi-from-shell"
        await pilot.press("enter")
        await pilot.pause()
        received = await _drain(bus)

    assert received, "expected ! to publish output as a user turn"
    assert "hi-from-shell" in received[0].content
    assert "echo hi-from-shell" in received[0].content


@pytest.mark.asyncio
async def test_double_bang_runs_silently_without_publishing(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "!!echo silent"
        await pilot.press("enter")
        await pilot.pause()
        received = await _drain(bus)
        # The bubble query must run INSIDE the with block — after teardown
        # the DOM is detached.
        assert received == [], "!! must not publish to the bus"
        bubbles = list(chat.query(MessageBubble))
        assert any(b._role == "system" and "silent" in b.body for b in bubbles)


@pytest.mark.asyncio
async def test_plain_text_unaffected_by_shell_paste(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "just a normal message"
        await pilot.press("enter")
        await pilot.pause()
        received = await _drain(bus)
    assert received and received[0].content == "just a normal message"
