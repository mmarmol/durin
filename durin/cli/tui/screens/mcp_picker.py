"""McpDiscoverScreen — searchable modal for discovering MCP servers.

Opened via ``/mcp`` (no args) or its key binding. Type a query, press Enter to
search the registry, then pick a row to add that server. Returns the chosen
server ``ref`` (or ``None`` on cancel) — the app performs the install, mirroring
how ``ModelPickerScreen`` returns an id the app commits.

Credential-bearing installs (local servers needing secrets) are completed via
``durin mcp install`` / the webUI; this screen covers discovery + quick-add.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

__all__ = ["McpDiscoverScreen"]

_KIND_TAG = {"remote": "no install", "both": "hosted/local", "local": "local"}


class McpDiscoverScreen(ModalScreen[str | None]):
    """Modal that returns the selected MCP server ref, or ``None`` on cancel."""

    BINDINGS = [
        Binding("escape", "dismiss_picker", "Cancel"),
    ]

    DEFAULT_CSS = """
    McpDiscoverScreen {
        align: center middle;
    }

    McpDiscoverScreen > Vertical {
        background: $surface;
        border: thick $accent;
        padding: 1 2;
        width: 80%;
        max-width: 100;
        height: 70%;
        max-height: 30;
    }

    McpDiscoverScreen Label.title {
        text-style: bold;
        padding: 0 0 1 0;
    }

    McpDiscoverScreen Label.hint {
        color: $text-muted;
        padding: 1 0 0 0;
    }

    McpDiscoverScreen #mcp-filter {
        margin: 0 0 1 0;
    }

    McpDiscoverScreen OptionList {
        height: 1fr;
    }

    McpDiscoverScreen #mcp-no-results {
        color: $text-muted;
        padding: 1 0;
    }
    """

    def __init__(self, search: Callable[[str], Awaitable[list[Any]]]) -> None:
        super().__init__()
        self._search = search
        self._hits: list[Any] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Discover MCP servers", classes="title")
            yield Input(
                placeholder="Search the registry (e.g. jira, postgres, github)…",
                id="mcp-filter",
            )
            yield OptionList(id="mcp-picker-list")
            yield Label("", id="mcp-no-results", classes="hint")
            yield Label(
                "Enter to search · ↑↓ + Enter to add · Esc to cancel", classes="hint"
            )

    def on_mount(self) -> None:
        self.query_one("#mcp-filter", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "mcp-filter":
            return
        query = event.value.strip()
        if not query:
            return
        await self._run_search(query)

    async def _run_search(self, query: str) -> None:
        ol = self.query_one("#mcp-picker-list", OptionList)
        no_results = self.query_one("#mcp-no-results", Label)
        no_results.update("Searching…")
        try:
            self._hits = await self._search(query)
        except Exception as exc:  # noqa: BLE001
            no_results.update(f"Search failed: {exc}")
            ol.display = False
            return
        ol.clear_options()
        if not self._hits:
            no_results.update("No servers found.")
            ol.display = False
            return
        no_results.update("")
        ol.display = True
        for hit in self._hits:
            tag = _KIND_TAG.get(hit.kind, hit.kind)
            label = f"{hit.ref}  [{tag}]"
            if hit.description:
                label += f"  — {hit.description}"
            ol.add_option(Option(label, id=hit.ref))
        ol.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss(event.option.id)

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
