"""ModelPickerScreen — fuzzy-searchable modal for switching models.

Opened via ``/model`` (no args) or Ctrl+L. Shows three sections:

★ Recent        — last 5 models switched to (persisted)
⚙ Configured    — presets from ``config.model_presets``
✦ Suggested     — curated ``DEFAULT_MODELS`` for configured providers

Typing in the filter input fuzzy-matches against model names in
real-time.  Pressing Enter with text that doesn't match any option
dismisses with the raw text — the caller creates a temp preset.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

from durin.cli.tui.fuzzy import fuzzy_match
from durin.cli.tui.model_catalog import ModelEntry, format_entry

__all__ = ["ModelPickerScreen"]

_HEADER_RECENT = "★ Recent"
_HEADER_PRESETS = "⚙ Configured"
_HEADER_SUGGESTED = "✦ Suggested"


class ModelPickerScreen(ModalScreen[str | None]):
    """Modal that returns the selected model name, or ``None`` on cancel."""

    BINDINGS = [
        Binding("escape", "dismiss_picker", "Cancel"),
    ]

    DEFAULT_CSS = """
    ModelPickerScreen {
        align: center middle;
    }

    ModelPickerScreen > Vertical {
        background: $surface;
        border: thick $accent;
        padding: 1 2;
        width: 80%;
        max-width: 100;
        height: 70%;
        max-height: 30;
    }

    ModelPickerScreen Label.title {
        text-style: bold;
        padding: 0 0 1 0;
    }

    ModelPickerScreen Label.hint {
        color: $text-muted;
        padding: 1 0 0 0;
    }

    ModelPickerScreen #model-filter {
        margin: 0 0 1 0;
    }

    ModelPickerScreen OptionList {
        height: 1fr;
    }

    ModelPickerScreen #no-results {
        color: $text-muted;
        padding: 1 0;
    }

    ModelPickerScreen Option.disabled {
        color: $accent;
        text-style: bold;
    }
    """

    def __init__(self, entries: list[ModelEntry], active: str = "default") -> None:
        super().__init__()
        self._entries = entries
        self._active = active
        self._dismiss_result: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Switch model", classes="title")
            yield Input(placeholder="Filter or type a model name…", id="model-filter")
            yield OptionList(id="model-picker-list")
            yield Label("", id="no-results", classes="hint")
            yield Label("Enter to switch · type to filter · Esc to cancel", classes="hint")

    def on_mount(self) -> None:
        self._populate_options("")
        self.query_one("#model-filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "model-filter":
            self._populate_options(event.value)

    def _populate_options(self, query: str) -> None:
        ol = self.query_one("#model-picker-list", OptionList)
        ol.clear_options()
        no_results = self.query_one("#no-results", Label)

        query_lower = query.strip()
        matched: list[tuple[str, ModelEntry]] = []
        for entry in self._entries:
            if query_lower and not fuzzy_match(query_lower, entry.name):
                continue
            matched.append((entry.group or _HEADER_SUGGESTED, entry))

        if not matched:
            no_results.update("No models match. Press Enter to use this name.")
            ol.display = False
            return

        no_results.update("")
        ol.display = True

        sections: list[tuple[str, list[ModelEntry]]] = []
        current_header = ""
        current_list: list[ModelEntry] = []
        for header, entry in matched:
            if header != current_header:
                if current_list:
                    sections.append((current_header, current_list))
                current_header = header
                current_list = [entry]
            else:
                current_list.append(entry)
        if current_list:
            sections.append((current_header, current_list))

        seen_ids: set[str] = set()
        for header, entries in sections:
            ol.add_option(Option(header, id=f"__header__{header}", disabled=True))
            for entry in entries:
                if entry.name in seen_ids:
                    continue
                seen_ids.add(entry.name)
                marker = " ← active" if entry.name == self._active else ""
                label = format_entry(entry) + marker
                ol.add_option(Option(label, id=entry.name))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id and not event.option.id.startswith("__header__"):
            self._dismiss_result = event.option.id
            self.dismiss(event.option.id)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        ol = self.query_one("#model-picker-list", OptionList)
        for i in range(ol.option_count):
            opt = ol.get_option_at_index(i)
            if opt.id == text:
                self._dismiss_result = text
                self.dismiss(text)
                return
        self._dismiss_result = text
        self.dismiss(text)

    def action_dismiss_picker(self) -> None:
        self._dismiss_result = None
        self.dismiss(None)
