"""Tests for the variant picker screen."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList


class _HostApp(App[None]):
    def compose(self) -> ComposeResult:
        yield OptionList()


@pytest.mark.asyncio
async def test_variant_picker_composes() -> None:
    from durin.cli.tui.screens.variant_picker import VariantPickerScreen

    screen = VariantPickerScreen()
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        ol = screen.query_one("#variant-list", OptionList)
        assert ol.option_count == 6


@pytest.mark.asyncio
async def test_variant_picker_marks_active() -> None:
    from durin.cli.tui.screens.variant_picker import VariantPickerScreen

    screen = VariantPickerScreen(active="high")
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        ol = screen.query_one("#variant-list", OptionList)
        # Find the "high" option and check it has the active marker.
        found = False
        for i in range(ol.option_count):
            opt = ol.get_option_at_index(i)
            if opt.id == "high":
                assert "← active" in opt.prompt
                found = True
                break
        assert found


@pytest.mark.asyncio
async def test_variant_picker_has_default_and_levels() -> None:
    from durin.cli.tui.screens.variant_picker import VariantPickerScreen

    screen = VariantPickerScreen()
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        ol = screen.query_one("#variant-list", OptionList)
        ids = set()
        for i in range(ol.option_count):
            opt = ol.get_option_at_index(i)
            if opt.id:
                ids.add(opt.id)
        assert "default" in ids
        assert "none" in ids
        assert "low" in ids
        assert "medium" in ids
        assert "high" in ids
        assert "max" in ids
