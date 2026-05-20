"""DurinApp — Textual TUI for durin.

D5.1 shipped the empty placeholder. D5.2 wires the layout: HeaderBar
across the top, ChatView taking the remaining vertical space,
InputArea above the FooterBar at the bottom. Streaming, slash
dispatch, modal pickers etc. land in D5.3+.

The legacy ``durin/cli/commands.py`` interactive path is untouched
and remains the default. The TUI is opt-in via ``durin agent --tui``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import __version__ as TEXTUAL_VERSION
from textual.app import App, ComposeResult
from textual.containers import Vertical

from durin import __version__ as DURIN_VERSION
from textual.widgets import Input

from durin.cli.tui.widgets import ChatView, FooterBar, HeaderBar, InputArea
from durin.cli.tui.widgets.footer_bar import payload_from_loop

__all__ = ["DurinApp", "run_durin_tui"]


class DurinApp(App[None]):
    """Top-level Textual App for durin."""

    TITLE = f"durin {DURIN_VERSION}"
    SUB_TITLE = f"Textual {TEXTUAL_VERSION}"

    CSS_PATH = "durin.tcss"

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

    # ---- composition ------------------------------------------------------

    def compose(self) -> ComposeResult:
        workspace = self._workspace_path()
        model, preset = self._model_label()
        with Vertical(id="main-layout"):
            yield HeaderBar(
                workspace_path=workspace,
                model=model,
                preset=preset,
            )
            yield ChatView(id="chat")
            yield InputArea(placeholder="Type a message · Ctrl+Q to quit")
            yield FooterBar(
                payload_getter=lambda: payload_from_loop(
                    self._agent_loop, self._cli_channel, self._cli_chat_id
                ),
            )

    # ---- event handlers (D5.2: echo only; streaming wires up in D5.3) ----

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Echo the user's submission into the chat view.

        D5.3 will replace this echo with a real ``InboundMessage`` publish
        through the ``MessageBus`` so the agent processes the turn.
        """
        value = event.value.strip()
        if not value:
            return
        # Clear the field so the next keystroke starts fresh.
        event.input.value = ""
        chat = self.query_one("#chat", ChatView)
        chat.add_message("user", value)
        chat.add_message(
            "system",
            "Streaming + agent dispatch land in D5.3 — see "
            "docs/10_textual_migration.md.",
        )

    # ---- helpers ----------------------------------------------------------

    def _workspace_path(self) -> str:
        if self._agent_loop is None:
            return ""
        try:
            return str(Path(self._agent_loop.workspace))
        except Exception:  # noqa: BLE001
            return ""

    def _model_label(self) -> tuple[str, str]:
        if self._agent_loop is None:
            return "?", "default"
        return (
            getattr(self._agent_loop, "model", "?") or "?",
            getattr(self._agent_loop, "model_preset", None) or "default",
        )


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
