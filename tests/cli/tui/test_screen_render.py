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
