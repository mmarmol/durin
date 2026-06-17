"""D5.5 — modal picker tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.cli.tui.app import DurinApp
from durin.cli.tui.screens.model_picker import ModelPickerScreen
from durin.cli.tui.screens.session_picker import SessionEntry, SessionPickerScreen
from durin.cli.tui.widgets import ChatView, InputArea, MessageBubble
from durin.config.schema import ModelPresetConfig


def _fake_agent_loop(bus: MessageBus, tmp_path) -> SimpleNamespace:
    async def _idle_run() -> None:
        await asyncio.Event().wait()

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    loop = SimpleNamespace(
        bus=bus,
        workspace=str(tmp_path),
        model="m",
        model_preset="default",
        model_presets={
            "default": ModelPresetConfig(model="glm-5.2"),
            "fast": ModelPresetConfig(model="glm-5-turbo"),
        },
        context_window_tokens=200_000,
        sessions=SimpleNamespace(
            sessions_dir=sessions_dir,
            get_or_create=lambda key: SimpleNamespace(messages=[], metadata={}),
        ),
        run=_idle_run,
    )
    return loop


def _write_session(sessions_dir, key: str, *, messages: int = 0, display_name: str = "") -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    safe_key = key.replace(":", "_")
    path = sessions_dir / f"{safe_key}.jsonl"
    metadata: dict = {"display_name": display_name} if display_name else {}
    lines = [
        json.dumps({
            "_type": "metadata",
            "key": key,
            "updated_at": "2026-05-20T10:00:00",
            "metadata": metadata,
        }),
    ]
    for i in range(messages):
        lines.append(json.dumps({"role": "user", "content": f"msg-{i}"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _drain(bus: MessageBus, *, timeout: float = 0.5) -> list[InboundMessage]:
    received: list[InboundMessage] = []
    while True:
        try:
            msg = await asyncio.wait_for(bus.consume_inbound(), timeout=timeout)
        except asyncio.TimeoutError:
            return received
        received.append(msg)


# ---------------------------------------------------------------------------
# Screens construct + render
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_picker_renders_entries() -> None:
    entries = [
        SessionEntry(key="cli:alpha", display_name="", msg_count=2, updated_at="2026-05-20T09:00:00"),
        SessionEntry(key="cli:beta", display_name="my-proj", msg_count=10, updated_at="2026-05-20T10:00:00"),
    ]
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        screen = SessionPickerScreen(entries, current_key="cli:alpha")
        app.push_screen(screen)
        await pilot.pause()
        # The picker shows both keys plus the current marker.
        from textual.widgets import OptionList
        opts = app.screen.query_one(OptionList)
        labels = [str(opts.get_option_at_index(i).prompt) for i in range(opts.option_count)]
        assert any("cli:alpha" in lab and "← current" in lab for lab in labels)
        assert any("cli:beta" in lab and "my-proj" in lab for lab in labels)
        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_model_picker_renders_presets() -> None:
    from durin.cli.tui.model_catalog import ModelEntry
    from durin.providers.capabilities import ModelCapabilities

    entries = [
        ModelEntry("default", "auto", True, False, ModelCapabilities(model="default")),
        ModelEntry("fast", "auto", True, False, ModelCapabilities(model="fast")),
        ModelEntry("opus", "auto", True, False, ModelCapabilities(model="opus")),
    ]
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        screen = ModelPickerScreen(entries, active="fast")
        app.push_screen(screen)
        await pilot.pause()
        from textual.widgets import OptionList
        opts = app.screen.query_one("#model-picker-list", OptionList)
        labels = [str(opts.get_option_at_index(i).prompt) for i in range(opts.option_count)]
        assert any("fast" in lab and "← active" in lab for lab in labels)
        assert any("opus" in lab for lab in labels)
        await pilot.press("escape")
        await pilot.pause()


# ---------------------------------------------------------------------------
# Slash-command interception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_slash_sessions_opens_picker(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    _write_session(loop.sessions.sessions_dir, "cli:alpha")
    _write_session(loop.sessions.sessions_dir, "cli:beta")

    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "/sessions"
        await pilot.press("enter")
        await pilot.pause()
        # Modal pushed → an OptionList is now in the focus chain.
        from textual.widgets import OptionList
        try:
            opts = app.screen.query_one(OptionList)
            opened = True
        except Exception:
            opened = False
        assert opened, "/sessions (bare) should open the SessionPickerScreen"
        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_bare_slash_model_opens_picker(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "/model"
        await pilot.press("enter")
        await pilot.pause()
        from textual.widgets import OptionList
        opts = app.screen.query_one(OptionList)
        labels = [str(opts.get_option_at_index(i).prompt) for i in range(opts.option_count)]
        # Preset rows show the model (with provider), opencode-style.
        assert any("glm-5.2" in l for l in labels)
        assert any("glm-5-turbo" in l for l in labels)
        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_slash_sessions_with_filter_still_publishes(tmp_path) -> None:
    """`/sessions <filter>` keeps the inline behaviour (no modal)."""
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "/sessions alpha"
        await pilot.press("enter")
        await pilot.pause()
        received = await _drain(bus)
    assert any(m.content == "/sessions alpha" for m in received)


@pytest.mark.asyncio
async def test_slash_model_with_preset_still_publishes(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "/model fast"
        await pilot.press("enter")
        await pilot.pause()
        received = await _drain(bus)
    assert any(m.content == "/model fast" for m in received)


@pytest.mark.asyncio
async def test_ctrl_l_opens_model_picker(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+l")
        await pilot.pause()
        from textual.widgets import OptionList
        try:
            app.screen.query_one(OptionList)
            opened = True
        except Exception:
            opened = False
        assert opened
        await pilot.press("escape")
        await pilot.pause()


# ---------------------------------------------------------------------------
# Selection flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_picker_selection_publishes_resume(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    _write_session(loop.sessions.sessions_dir, "cli:alpha")
    _write_session(loop.sessions.sessions_dir, "cli:beta")

    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "/sessions"
        await pilot.press("enter")
        await pilot.pause()
        # Pick the first option (Enter on the OptionList)
        from textual.widgets import OptionList
        opts = app.screen.query_one(OptionList)
        opts.focus()
        await pilot.press("enter")
        await pilot.pause()
        received = await _drain(bus)
    assert any(m.content.startswith("/resume ") for m in received), [m.content for m in received]


@pytest.mark.asyncio
async def test_empty_sessions_shows_message(tmp_path) -> None:
    bus = MessageBus()
    loop = _fake_agent_loop(bus, tmp_path)
    app = DurinApp(agent_loop=loop)
    async with app.run_test() as pilot:
        inp = app.query_one(InputArea)
        inp.focus()
        await pilot.pause()
        inp.value = "/sessions"
        await pilot.press("enter")
        await pilot.pause()
        chat = app.query_one(ChatView)
        bubbles = [b for b in chat.query(MessageBubble) if b._role == "system"]
        assert any("No sessions" in b.body for b in bubbles)
