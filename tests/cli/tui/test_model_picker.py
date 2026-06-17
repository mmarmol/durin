"""Tests for the enhanced ModelPickerScreen."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList

from durin.cli.tui.model_catalog import ModelEntry
from durin.providers.capabilities import ModelCapabilities


def _make_entry(name, *, group="Easy pick", is_preset=False, is_recent=False):
    return ModelEntry(
        name=name,
        provider="auto",
        is_preset=is_preset,
        is_recent=is_recent,
        capabilities=ModelCapabilities(model=name),
        group=group,
    )


@pytest.fixture
def entries():
    return [
        _make_entry("glm-5.2", group="Easy pick", is_recent=True),
        _make_entry("default", group="Easy pick", is_preset=True),
        _make_entry("claude-sonnet-4-6", group="anthropic"),
    ]


class _HostApp(App[None]):
    """Minimal host app to mount a modal screen."""

    def compose(self) -> ComposeResult:
        yield Input()


async def test_screen_composes_without_error(entries):
    from durin.cli.tui.screens.model_picker import ModelPickerScreen

    screen = ModelPickerScreen(entries, active="default")
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        assert pilot.app.screen is screen


async def test_screen_shows_all_sections_when_empty_filter(entries):
    from durin.cli.tui.screens.model_picker import ModelPickerScreen

    screen = ModelPickerScreen(entries, active="default")
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        ol = screen.query_one("#model-picker-list", OptionList)
        # 2 group headers ("Easy pick", "anthropic") + 3 model rows.
        assert ol.option_count == 5


async def test_screen_filters_by_fuzzy_query(entries):
    from durin.cli.tui.screens.model_picker import ModelPickerScreen

    screen = ModelPickerScreen(entries, active="default")
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#model-filter", Input).value = "claude"
        await pilot.pause()
        ol = screen.query_one("#model-picker-list", OptionList)
        assert ol.option_count == 2


async def test_screen_shows_no_results_message(entries):
    from durin.cli.tui.screens.model_picker import ModelPickerScreen

    screen = ModelPickerScreen(entries, active="default")
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#model-filter", Input).value = "zzzznomatch"
        await pilot.pause()
        ol = screen.query_one("#model-picker-list", OptionList)
        assert ol.option_count == 0
        assert screen.query_one("#no-results")


async def test_screen_dismisses_with_free_form_text(entries):
    from durin.cli.tui.screens.model_picker import ModelPickerScreen

    screen = ModelPickerScreen(entries, active="default")
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#model-filter", Input).value = "my-custom-model"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert screen._dismiss_result == "my-custom-model"
