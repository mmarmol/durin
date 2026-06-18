"""Tests for McpDiscoverScreen (TUI MCP discovery)."""
from __future__ import annotations

from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList

from durin.cli.tui.screens.mcp_picker import McpDiscoverScreen


@dataclass
class _Hit:
    ref: str
    kind: str
    description: str = ""


class _HostApp(App[None]):
    def compose(self) -> ComposeResult:
        yield Input()


async def _fake_search(query):
    return [
        _Hit("io.x/jira", "remote", "Jira issues"),
        _Hit("io.x/postgres", "local", "Postgres database"),
    ]


async def test_screen_composes():
    screen = McpDiscoverScreen(_fake_search)
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        assert pilot.app.screen is screen


async def test_search_populates_options():
    screen = McpDiscoverScreen(_fake_search)
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        inp = screen.query_one("#mcp-filter", Input)
        inp.value = "jira"
        inp.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        ol = screen.query_one("#mcp-picker-list", OptionList)
        assert ol.option_count == 2
        ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
        assert ids == ["io.x/jira", "io.x/postgres"]


async def test_no_results_message():
    async def _empty(query):
        return []

    screen = McpDiscoverScreen(_empty)
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        inp = screen.query_one("#mcp-filter", Input)
        inp.value = "zzz"
        inp.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        ol = screen.query_one("#mcp-picker-list", OptionList)
        assert ol.option_count == 0
        assert ol.display is False  # list hidden when there are no results
