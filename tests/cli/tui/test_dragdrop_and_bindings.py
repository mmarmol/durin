"""D5.6 + D5.7 — drag-and-drop pre-processing + key-binding actions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.cli.tui.app import DurinApp
from durin.cli.tui.widgets import ChatView, InputArea, MessageBubble


def _fake_agent_loop(bus: MessageBus, tmp_path) -> SimpleNamespace:
    async def _idle_run() -> None:
        await asyncio.Event().wait()

    cancelled: list[str] = []

    async def _cancel_active_tasks(session_key: str) -> int:
        cancelled.append(session_key)
        return 1

    loop = SimpleNamespace(
        bus=bus,
        workspace=str(tmp_path),
        model="m",
        model_preset="default",
        context_window_tokens=200_000,
        sessions=SimpleNamespace(
            get_or_create=lambda key: SimpleNamespace(messages=[], metadata={})
        ),
        run=_idle_run,
        _cancel_active_tasks=_cancel_active_tasks,
    )
    loop._cancelled = cancelled  # type: ignore[attr-defined]
    return loop


async def _drain(bus: MessageBus, *, timeout: float = 0.5) -> list[InboundMessage]:
    received: list[InboundMessage] = []
    while True:
        try:
            msg = await asyncio.wait_for(bus.consume_inbound(), timeout=timeout)
        except asyncio.TimeoutError:
            return received
        received.append(msg)


# ---------------------------------------------------------------------------
# D5.6 drag-and-drop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_path_is_copied_to_media_and_surfaces_in_inbound(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    # Source image lives OUTSIDE the workspace; dragdrop copies it into .media/.
    img = tmp_path.parent / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = f"check {img}"
        await pilot.press("enter")
        await pilot.pause()
        received = await _drain(bus)

    assert len(received) == 1
    msg = received[0]
    assert msg.media, "expected the image path to surface via InboundMessage.media"
    assert msg.media[0].startswith(".media/")
    # Cleaned text replaces the dragged path with the workspace-local copy.
    assert str(img) not in msg.content


@pytest.mark.asyncio
async def test_plain_text_bypasses_dragdrop(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "hola sin path"
        await pilot.press("enter")
        await pilot.pause()
        received = await _drain(bus)

    assert received and received[0].media == []
    assert received[0].content == "hola sin path"


# ---------------------------------------------------------------------------
# D5.7 key bindings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escape_invokes_cancel_active_tasks(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop, cli_chat_id="myproj")
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        # Simulate that a streaming reply was in flight.
        app._current_assistant_bubble = chat.add_message("assistant", "...")
        await pilot.press("escape")
        await pilot.pause()
    assert "cli:myproj" in loop._cancelled  # type: ignore[attr-defined]
    assert app._current_assistant_bubble is None


@pytest.mark.asyncio
async def test_ctrl_t_toggles_theme(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        original = app.theme
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme != original
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme == original


@pytest.mark.asyncio
async def test_ctrl_l_prefills_model_command(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert inp.value == "/model "
