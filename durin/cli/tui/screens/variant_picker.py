"""VariantPickerScreen — select reasoning effort level for the active model.

Opened via ``Ctrl+Shift+L`` or the command palette. Shows a simple list
of effort levels. Selecting one creates a temp preset variant with the
same model but different ``reasoning_effort`` and dismisses with the
effort string (or ``None`` for provider default).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

__all__ = ["VariantPickerScreen"]

# (effort, label, description)
_VARIANTS: list[tuple[str | None, str, str]] = [
    (None, "Default", "Provider default (no override)"),
    ("none", "Off", "Explicitly disable reasoning"),
    ("low", "Low", "Fast, minimal thinking"),
    ("medium", "Medium", "Balanced thinking"),
    ("high", "High", "Deep reasoning"),
    ("max", "Max", "Maximum reasoning (if supported)"),
]


class VariantPickerScreen(ModalScreen[str | None]):
    """Modal that returns the selected effort string, or ``None`` on cancel.

    The ``None`` return is ambiguous between "cancel" and "default effort".
    To distinguish, the caller should check ``_dismiss_result`` — but
    since Textual's ``dismiss`` only passes the value, we use a sentinel:

    - Dismiss with ``""`` (empty string) → cancelled
    - Dismiss with ``"default"`` → provider default (effort=None)
    - Dismiss with ``"none"`` / ``"low"`` / ``"medium"`` / ``"high"`` / ``"max"``
      → that effort level
    """

    _CANCEL_SENTINEL = "__cancel__"

    BINDINGS = [
        Binding("escape", "dismiss_picker", "Cancel"),
    ]

    DEFAULT_CSS = """
    VariantPickerScreen {
        align: center middle;
    }

    VariantPickerScreen > Vertical {
        background: $surface;
        border: thick $accent;
        padding: 1 2;
        width: 60%;
        max-width: 70;
        height: 60%;
        max-height: 20;
    }

    VariantPickerScreen Label.title {
        text-style: bold;
        padding: 0 0 1 0;
    }

    VariantPickerScreen Label.hint {
        color: $text-muted;
        padding: 1 0 0 0;
    }

    VariantPickerScreen OptionList {
        height: 1fr;
    }
    """

    def __init__(self, active: str | None = None) -> None:
        super().__init__()
        self._active = active
        self._dismiss_result: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Reasoning effort", classes="title")
            yield OptionList(id="variant-list")
            yield Label("Enter to select · Esc to cancel", classes="hint")

    def on_mount(self) -> None:
        ol = self.query_one("#variant-list", OptionList)
        for effort, label, desc in _VARIANTS:
            key = effort if effort is not None else "default"
            marker = " ← active" if key == self._active_label() else ""
            ol.add_option(Option(f"{label} — {desc}{marker}", id=key))

    def _active_label(self) -> str:
        if self._active is None:
            return "default"
        return self._active

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option.id:
            self._dismiss_result = event.option.id
            self.dismiss(event.option.id)

    def action_dismiss_picker(self) -> None:
        self._dismiss_result = self._CANCEL_SENTINEL
        self.dismiss(self._CANCEL_SENTINEL)
