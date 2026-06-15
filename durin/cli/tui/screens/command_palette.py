"""CommandPaletteScreen — fuzzy-searchable palette of all TUI commands.

Opened via ``Ctrl+P``. Shows two sections:

  ⌘ Commands  — slash commands (``/model``, ``/new``, etc.)
  ⚡ Actions   — quick TUI actions (open model picker, toggle theme, etc.)

Typing in the filter input fuzzy-matches against labels in real-time.
Selecting a command dismisses with its ``id`` — the caller decides
whether to publish it as a slash command or trigger an action.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

from durin.cli.tui.command_registry import CommandEntry, build_command_entries
from durin.cli.tui.fuzzy import fuzzy_match

__all__ = ["CommandPaletteScreen"]

_HEADER_COMMANDS = "⌘ Commands"
_HEADER_ACTIONS = "⚡ Actions"


class CommandPaletteScreen(ModalScreen[str | None]):
    """Modal that returns the selected entry id, or ``None`` on cancel."""

    BINDINGS = [
        Binding("escape", "dismiss_picker", "Cancel"),
    ]

    DEFAULT_CSS = """
    CommandPaletteScreen {
        align: center middle;
    }

    CommandPaletteScreen > Vertical {
        background: $surface;
        border: thick $accent;
        padding: 1 2;
        width: 80%;
        max-width: 100;
        height: 70%;
        max-height: 30;
    }

    CommandPaletteScreen Label.title {
        text-style: bold;
        padding: 0 0 1 0;
    }

    CommandPaletteScreen Label.hint {
        color: $text-muted;
        padding: 1 0 0 0;
    }

    CommandPaletteScreen #palette-filter {
        margin: 0 0 1 0;
    }

    CommandPaletteScreen OptionList {
        height: 1fr;
    }

    CommandPaletteScreen #palette-no-results {
        color: $text-muted;
        padding: 1 0;
    }

    CommandPaletteScreen Option.disabled {
        color: $accent;
        text-style: bold;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[CommandEntry] = build_command_entries()
        self._dismiss_result: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Command palette", classes="title")
            yield Input(placeholder="Type a command or action…", id="palette-filter")
            yield OptionList(id="palette-list")
            yield Label("", id="palette-no-results", classes="hint")
            yield Label(
                "Enter to run · type to filter · Esc to cancel", classes="hint"
            )

    def on_mount(self) -> None:
        self._populate("")
        self.query_one("#palette-filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "palette-filter":
            self._populate(event.value)

    def _populate(self, query: str) -> None:
        ol = self.query_one("#palette-list", OptionList)
        ol.clear_options()
        no_results = self.query_one("#palette-no-results", Label)

        q = query.strip()
        matched: list[tuple[str, CommandEntry]] = []
        for entry in self._entries:
            if q and not fuzzy_match(q, entry.label):
                continue
            header = _HEADER_COMMANDS if entry.kind == "command" else _HEADER_ACTIONS
            matched.append((header, entry))

        if not matched:
            no_results.update("No commands match.")
            ol.display = False
            return

        no_results.update("")
        ol.display = True

        current_header = ""
        for header, entry in matched:
            if header != current_header:
                ol.add_option(
                    Option(header, id=f"__header__{header}", disabled=True)
                )
                current_header = header
            shortcut = f"  [{entry.shortcut}]" if entry.shortcut else ""
            ol.add_option(Option(f"{entry.label}{shortcut}", id=entry.id))

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option.id and not event.option.id.startswith("__header__"):
            self._dismiss_result = event.option.id
            self.dismiss(event.option.id)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        ol = self.query_one("#palette-list", OptionList)
        # If the typed text matches a command name exactly, run it.
        for i in range(ol.option_count):
            opt = ol.get_option_at_index(i)
            if opt.id and opt.id == f"cmd:{text}":
                self._dismiss_result = opt.id
                self.dismiss(opt.id)
                return
        # Otherwise, if it looks like a slash command, publish it as-is.
        if text.startswith("/"):
            self._dismiss_result = f"cmd:{text}"
            self.dismiss(f"cmd:{text}")

    def action_dismiss_picker(self) -> None:
        self._dismiss_result = None
        self.dismiss(None)
