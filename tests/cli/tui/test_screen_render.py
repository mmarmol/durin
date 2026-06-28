"""Rendered-output tests for the durin TUI.

Most TUI tests query the widget tree; these complement them by reading
the compositor strips through :mod:`durin.cli.tui.probe` — the same path
the agent uses via ``scripts/tui_smoke.py``. They also guard the boot
contract: a chat surface must be typable the instant it opens.
"""

from __future__ import annotations

import pytest

from durin.cli.tui.app import DurinApp
from durin.cli.tui.probe import run_step, screen_text, type_text
from durin.cli.tui.widgets import InputArea


@pytest.mark.asyncio
async def test_boot_screen_shows_welcome() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        text = screen_text(app)
    assert "durin" in text
    assert "Type a message" in text
    assert "message durin" in text  # the input placeholder


@pytest.mark.asyncio
async def test_input_is_focused_on_boot() -> None:
    """The input must hold focus on launch — otherwise keystrokes fall
    through to the scrollable history and are silently swallowed."""
    app = DurinApp(agent_loop=None)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        assert app.query_one(InputArea).has_focus


@pytest.mark.asyncio
async def test_typing_appears_on_screen() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await type_text(pilot, "hello durin")
        await pilot.pause()
        text = screen_text(app)
    assert "hello durin" in text


@pytest.mark.asyncio
async def test_run_step_drives_typing() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await run_step(pilot, "type:/help")
        text = screen_text(app)
    assert "/help" in text


@pytest.mark.asyncio
async def test_run_step_rejects_unknown_verb() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        with pytest.raises(ValueError):
            await run_step(pilot, "frobnicate:x")


def test_user_bubble_exposes_edit_payload() -> None:
    from durin.cli.tui.widgets.chat_view import MessageBubble

    bubble = MessageBubble(role="user", body="original text")
    assert bubble.editable_text() == "original text"


def test_quick_actions_present() -> None:
    from durin.cli.tui.widgets.chat_view import ChatView

    assert ChatView.quick_actions() == ["Plan", "Analyze", "Brainstorm", "Code", "Summarize"]


@pytest.mark.asyncio
async def test_scroll_to_bottom_button_visibility() -> None:
    """watch_scroll_y toggles the scroll button: visible when scrolled up, hidden at bottom."""
    from durin.cli.tui.widgets.chat_view import ChatView, _ScrollToBottom

    app = DurinApp(agent_loop=None)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        chat = app.query_one(ChatView)

        # Add enough messages to create scrollback.
        for i in range(30):
            chat.add_message("assistant", f"Message {i}: " + "x" * 60)
        await pilot.pause()

        # Scroll to bottom first so max_scroll_y is known.
        chat.scroll_end(animate=False)
        await pilot.pause()

        # Simulate scrolling up: set scroll_y below max so the button should appear.
        max_y = chat.max_scroll_y
        if max_y > 2:
            chat.scroll_y = 0
            await pilot.pause()
            btn = chat.query_one("#scroll-to-bottom", _ScrollToBottom)
            assert btn.display is True, "button should be visible when scrolled up"

        # Scroll back to bottom: button should hide.
        chat.scroll_end(animate=False)
        await pilot.pause()
        btn = chat.query_one("#scroll-to-bottom", _ScrollToBottom)
        assert btn.display is False, "button should be hidden when at bottom"


@pytest.mark.asyncio
async def test_chips_visible_for_banner_messages() -> None:
    """Chips should remain visible when adding decorative banner/logo messages."""
    from durin.cli.tui.widgets.chat_view import ChatView

    app = DurinApp(agent_loop=None)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        chat = app.query_one(ChatView)
        chips = chat.query_one("#qa-chips")
        # Chips should be visible initially
        assert chips.display is True
        # Add banner message
        chat.add_message("banner", "Welcome to durin")
        await pilot.pause()
        # Chips should still be visible after banner
        assert chips.display is True
        # Add logo message
        chat.add_message("logo", "durin ASCII art")
        await pilot.pause()
        # Chips should still be visible after logo
        assert chips.display is True
        # Add real user message
        chat.add_message("user", "hello")
        await pilot.pause()
        # Now chips should be hidden
        assert chips.display is False
