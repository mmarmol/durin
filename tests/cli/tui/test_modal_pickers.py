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
        ModelEntry("default", "auto", True, False, ModelCapabilities(model="default"), ref="default"),
        ModelEntry("fast", "auto", True, False, ModelCapabilities(model="fast"), ref="fast"),
        ModelEntry("opus", "auto", True, False, ModelCapabilities(model="opus"), ref="opus"),
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


# ---------------------------------------------------------------------------
# Landing chips (ChatView.session_chips + _QuickActionChips)
# ---------------------------------------------------------------------------


def _fake_agent_loop_with_manager(bus: MessageBus, tmp_path) -> SimpleNamespace:
    """Like `_fake_agent_loop`, but backs `sessions` with a real SessionManager
    so `list_sessions()` (title/preview) works — the landing chips read that,
    not the raw jsonl walk the session picker uses."""
    async def _idle_run() -> None:
        await asyncio.Event().wait()

    from durin.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    return SimpleNamespace(
        bus=bus,
        workspace=str(tmp_path),
        model="m",
        model_preset="default",
        model_presets={"default": ModelPresetConfig(model="glm-5.2")},
        context_window_tokens=200_000,
        sessions=manager,
        run=_idle_run,
    )


def _write_titled_session(
    sessions_dir, key: str, *, updated_at: str, title: str = "", preview_text: str = ""
) -> None:
    """Write a session jsonl with a `title` in metadata (what SessionManager.list_sessions
    reads) and an optional user message so the preview-fallback path has content."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    safe_key = key.replace(":", "_")
    path = sessions_dir / f"{safe_key}.jsonl"
    metadata: dict = {"title": title} if title else {}
    lines = [
        json.dumps({
            "_type": "metadata",
            "key": key,
            "updated_at": updated_at,
            "metadata": metadata,
        }),
    ]
    if preview_text:
        lines.append(json.dumps({"role": "user", "content": preview_text}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_session_chips_use_title_and_preview_fallback(tmp_path) -> None:
    from durin.cli.tui.widgets.chat_view import ChatView

    bus = MessageBus()
    loop = _fake_agent_loop_with_manager(bus, tmp_path)
    sessions_dir = loop.sessions.sessions_dir
    _write_titled_session(sessions_dir, "cli:alpha", updated_at="2026-05-20T12:00:00", title="Alpha project")
    _write_titled_session(
        sessions_dir, "cli:beta", updated_at="2026-05-20T11:00:00", preview_text="what is the plan"
    )

    app = DurinApp(agent_loop=loop, cli_channel="cli", cli_chat_id="current")
    async with app.run_test() as pilot:
        await pilot.pause()
        chips = ChatView.session_chips(app)
    assert chips[0] == ("Resume: Alpha project", "cli:alpha")
    assert chips[1] == ("Continue: what is the plan", "cli:beta")


@pytest.mark.asyncio
async def test_session_chips_skip_current_session(tmp_path) -> None:
    from durin.cli.tui.widgets.chat_view import ChatView

    bus = MessageBus()
    loop = _fake_agent_loop_with_manager(bus, tmp_path)
    sessions_dir = loop.sessions.sessions_dir
    _write_titled_session(sessions_dir, "cli:current", updated_at="2026-05-20T12:00:00", title="Current one")
    _write_titled_session(sessions_dir, "cli:alpha", updated_at="2026-05-20T11:00:00", title="Alpha project")

    app = DurinApp(agent_loop=loop, cli_channel="cli", cli_chat_id="current")
    async with app.run_test() as pilot:
        await pilot.pause()
        chips = ChatView.session_chips(app)
    assert chips == [("Resume: Alpha project", "cli:alpha")]


@pytest.mark.asyncio
async def test_audit_chip_always_present_with_no_sessions(tmp_path) -> None:
    from durin.cli.tui.widgets.chat_view import ChatView, _QuickActionChips

    bus = MessageBus()
    loop = _fake_agent_loop_with_manager(bus, tmp_path)

    app = DurinApp(agent_loop=loop, cli_channel="cli", cli_chat_id="current")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ChatView.session_chips(app) == []
        chips_widget = app.query_one(_QuickActionChips)
        chip_texts = [str(c._Static__content) for c in chips_widget.query(".qa-chip")]
    assert chip_texts == [ChatView.AUDIT_LABEL]


@pytest.mark.asyncio
async def test_clicking_audit_chip_publishes_audit(tmp_path) -> None:
    from durin.cli.tui.widgets.chat_view import _QuickActionChips

    bus = MessageBus()
    loop = _fake_agent_loop_with_manager(bus, tmp_path)

    app = DurinApp(agent_loop=loop, cli_channel="cli", cli_chat_id="current")
    async with app.run_test() as pilot:
        await pilot.pause()
        chips_widget = app.query_one(_QuickActionChips)
        audit_chip = chips_widget.query_one(".qa-chip-audit")
        chips_widget.on_click(SimpleNamespace(widget=audit_chip))
        await pilot.pause()
        received = await _drain(bus)
    assert any(m.content == "/audit" for m in received)


@pytest.mark.asyncio
async def test_clicking_resume_chip_publishes_resume(tmp_path) -> None:
    from durin.cli.tui.widgets.chat_view import _QuickActionChips

    bus = MessageBus()
    loop = _fake_agent_loop_with_manager(bus, tmp_path)
    sessions_dir = loop.sessions.sessions_dir
    _write_titled_session(sessions_dir, "cli:alpha", updated_at="2026-05-20T11:00:00", title="Alpha project")

    app = DurinApp(agent_loop=loop, cli_channel="cli", cli_chat_id="current")
    async with app.run_test() as pilot:
        await pilot.pause()
        chips_widget = app.query_one(_QuickActionChips)
        resume_chip = chips_widget.query_one(".qa-chip-resume")
        chips_widget.on_click(SimpleNamespace(widget=resume_chip))
        await pilot.pause()
        received = await _drain(bus)
    assert any(m.content == "/resume cli:alpha" for m in received)


# ---------------------------------------------------------------------------
# Persona picker
# ---------------------------------------------------------------------------


def test_persona_picker_builds_rows_and_marks_active():
    from durin.cli.tui.screens.persona_picker import PersonaPickerScreen, PersonaRow

    rows = [
        PersonaRow(name="default", soul="default", model=None),
        PersonaRow(name="reviewer", soul="critic", model="sonnet-4.6"),
    ]
    screen = PersonaPickerScreen(rows, active="reviewer")
    labels = [screen._format_row(r) for r in rows]
    assert any("reviewer" in lbl and "active" in lbl for lbl in labels)
    assert any("critic" in lbl for lbl in labels)
