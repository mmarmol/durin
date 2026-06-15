"""Tests for the command palette screen."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList


class _HostApp(App[None]):
    """Minimal host app to mount a modal screen."""

    def compose(self) -> ComposeResult:
        yield Input()


@pytest.mark.asyncio
async def test_palette_composes_widgets() -> None:
    """The palette should have an Input, OptionList, and populated options."""
    from durin.cli.tui.screens.command_palette import CommandPaletteScreen

    screen = CommandPaletteScreen()
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#palette-filter", Input) is not None
        ol = screen.query_one("#palette-list", OptionList)
        assert ol.option_count > 0


@pytest.mark.asyncio
async def test_palette_has_command_and_action_sections() -> None:
    """Both Commands and Actions section headers should be present."""
    from durin.cli.tui.screens.command_palette import CommandPaletteScreen

    screen = CommandPaletteScreen()
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        ol = screen.query_one("#palette-list", OptionList)
        ids = []
        for i in range(ol.option_count):
            opt = ol.get_option_at_index(i)
            if opt.id:
                ids.append(opt.id)
        assert any("__header__" in i and "Commands" in i for i in ids)
        assert any("__header__" in i and "Actions" in i for i in ids)


@pytest.mark.asyncio
async def test_palette_fuzzy_filter() -> None:
    """Typing should filter the list down."""
    from durin.cli.tui.screens.command_palette import CommandPaletteScreen

    screen = CommandPaletteScreen()
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        ol = screen.query_one("#palette-list", OptionList)
        full_count = ol.option_count

        screen.query_one("#palette-filter", Input).value = "model"
        await pilot.pause()

        assert ol.option_count < full_count
        assert ol.option_count > 0


@pytest.mark.asyncio
async def test_palette_filter_no_results() -> None:
    """A nonsensical query should hide the option list."""
    from durin.cli.tui.screens.command_palette import CommandPaletteScreen

    screen = CommandPaletteScreen()
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#palette-filter", Input).value = "zzzznotreal"
        await pilot.pause()
        ol = screen.query_one("#palette-list", OptionList)
        assert not ol.display


@pytest.mark.asyncio
async def test_palette_actions_include_model_and_theme() -> None:
    """Known actions should be in the list."""
    from durin.cli.tui.screens.command_palette import CommandPaletteScreen

    screen = CommandPaletteScreen()
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        ol = screen.query_one("#palette-list", OptionList)
        ids = set()
        for i in range(ol.option_count):
            opt = ol.get_option_at_index(i)
            if opt.id and not opt.id.startswith("__header__"):
                ids.add(opt.id)
        assert "act:open_model_picker" in ids
        assert "act:open_theme_picker" in ids


@pytest.mark.asyncio
async def test_command_registry_builds_entries() -> None:
    """The registry should produce a non-empty list with commands and actions."""
    from durin.cli.tui.command_registry import build_command_entries

    entries = build_command_entries()
    assert len(entries) > 10
    kinds = {e.kind for e in entries}
    assert "command" in kinds
    assert "action" in kinds
