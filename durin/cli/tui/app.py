"""DurinApp — Textual TUI scaffolding (D5.1).

This module is the entry point for the opt-in Textual UI. D5.1 ships
only the skeleton: a banner widget, a hint about layout that's coming
in D5.2, and clean Ctrl+Q exit. Subsequent sub-tasks fill in the
layout, streaming, slash commands, modals, etc.

The legacy ``durin/cli/commands.py`` interactive path is untouched.
Both modes run on the same ``AgentLoop`` + ``MessageBus``; the TUI
only swaps the I/O layer.
"""

from __future__ import annotations

from typing import Any

from textual import __version__ as TEXTUAL_VERSION
from textual.app import App, ComposeResult
from textual.containers import Center, Middle
from textual.widgets import Footer, Header, Static

from durin import __version__ as DURIN_VERSION

__all__ = ["DurinApp", "run_durin_tui"]


_BANNER = """\
[bold cyan]durin[/bold cyan] [dim]· Textual UI scaffolding (D5.1)[/dim]

The legacy CLI is still the default — this opt-in surface is empty
on purpose until the layout (D5.2), streaming integration (D5.3),
slash commands (D5.4), and modal pickers (D5.5) land.

[dim]Press Ctrl+Q to exit.[/dim]
"""


class DurinApp(App[None]):
    """Top-level Textual App for durin.

    Holds a reference to the live :class:`AgentLoop` so future
    sub-tasks (D5.3 onwards) can publish ``InboundMessage`` and
    consume ``OutboundMessage`` from the bus that drives the agent.
    """

    TITLE = f"durin {DURIN_VERSION}"
    SUB_TITLE = f"Textual {TEXTUAL_VERSION}"

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        agent_loop: Any | None = None,
        cli_channel: str = "cli",
        cli_chat_id: str = "direct",
        markdown: bool = True,
    ) -> None:
        super().__init__()
        self._agent_loop = agent_loop
        self._cli_channel = cli_channel
        self._cli_chat_id = cli_chat_id
        self._markdown = markdown

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Middle():
            with Center():
                yield Static(_BANNER, id="banner")
        yield Footer()


def run_durin_tui(
    *,
    agent_loop: Any | None,
    cli_channel: str = "cli",
    cli_chat_id: str = "direct",
    markdown: bool = True,
) -> None:
    """Launch the Textual app. Blocks until the user quits."""
    app = DurinApp(
        agent_loop=agent_loop,
        cli_channel=cli_channel,
        cli_chat_id=cli_chat_id,
        markdown=markdown,
    )
    app.run()
