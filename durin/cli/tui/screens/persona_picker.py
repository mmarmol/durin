"""Persona picker modal — opened via /persona or its binding.

Lists configured personas (name · soul · model), marks the active one, and
returns the chosen name so the app can publish ``/persona <name>``. Mirrors the
model picker's filter/select pattern.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

from durin.cli.tui.fuzzy import fuzzy_match

__all__ = ["PersonaPickerScreen", "PersonaRow"]


@dataclass
class PersonaRow:
    name: str
    soul: str
    model: str | None = None


class PersonaPickerScreen(ModalScreen[str | None]):
    """Modal that returns the selected persona name, or None on cancel."""

    BINDINGS = [Binding("escape", "dismiss_picker", "Cancel")]

    DEFAULT_CSS = """
    PersonaPickerScreen { align: center middle; }
    PersonaPickerScreen > Vertical {
        width: 60; height: auto; max-height: 80%;
        background: $surface; border: round $accent; padding: 1 2;
    }
    PersonaPickerScreen .title { text-style: bold; color: $accent; }
    PersonaPickerScreen .hint { color: $text-muted; text-style: italic; }
    """

    def __init__(self, rows: list[PersonaRow], active: str = "default") -> None:
        super().__init__()
        self._rows = rows
        self._active = active

    def _format_row(self, row: PersonaRow) -> str:
        model = row.model or "default model"
        marker = " ← active" if row.name == self._active else ""
        return f"{row.name} · soul: {row.soul} · {model}{marker}"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Switch persona", classes="title")
            yield Input(placeholder="Filter by name…", id="persona-filter")
            yield OptionList(id="persona-list")
            yield Label("Enter to switch · type to filter · Esc to cancel", classes="hint")

    def on_mount(self) -> None:
        self._populate("")
        self.query_one("#persona-filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "persona-filter":
            self._populate(event.value)

    def _populate(self, query: str) -> None:
        ol = self.query_one("#persona-list", OptionList)
        ol.clear_options()
        q = query.strip()
        for row in self._rows:
            if q and not fuzzy_match(q, row.name):
                continue
            ol.add_option(Option(self._format_row(row), id=row.name))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss(event.option.id)

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
