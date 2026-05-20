"""ModelPickerScreen — modal that lets the user pick a model preset.

Opened via the ``/model`` slash command (when typed with no preset
argument) or via Ctrl+L. Selecting an option returns the preset name
to the App, which then publishes ``/model <preset>`` through the bus
so the existing CommandRouter handles the actual switch.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

__all__ = ["ModelPickerScreen"]


class ModelPickerScreen(ModalScreen[str | None]):
    """Modal that returns the selected preset name, or ``None`` on cancel."""

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
        width: 60%;
        max-width: 80;
        height: 60%;
        max-height: 22;
    }

    ModelPickerScreen Label.title {
        text-style: bold;
        padding: 0 0 1 0;
    }

    ModelPickerScreen Label.hint {
        color: $text-muted;
        padding: 1 0 0 0;
    }

    ModelPickerScreen OptionList {
        height: 1fr;
    }
    """

    def __init__(self, presets: list[str], active: str = "default") -> None:
        super().__init__()
        self._presets = presets
        self._active = active

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Pick a model preset", classes="title")
            options = []
            for name in self._presets:
                marker = " ← active" if name == self._active else ""
                options.append(Option(f"{name}{marker}", id=name))
            yield OptionList(*options, id="model-picker-list")
            yield Label("Enter to switch · Esc to cancel", classes="hint")

    def on_mount(self) -> None:
        try:
            self.query_one("#model-picker-list", OptionList).focus()
        except Exception:  # noqa: BLE001
            pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
