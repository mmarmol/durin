"""ThemePickerScreen — modal to pick durin's colour palette.

Opened via the ``/theme`` slash command. Returns the selected palette
name (``ithildin`` / ``forge`` / ``mithril``); the App applies it and
persists it to config. Light/dark mode is a separate toggle (Ctrl+T).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

__all__ = ["ThemePickerScreen"]

# palette name -> one-line description (see design/DESIGN.md)
_PALETTES: list[tuple[str, str]] = [
    ("ithildin", "cool slate + sky-cyan"),
    ("forge", "warm near-black + ember"),
    ("mithril", "achromatic silver"),
]


class ThemePickerScreen(ModalScreen[str | None]):
    """Modal that returns the chosen palette name, or ``None`` on cancel."""

    BINDINGS = [
        Binding("escape", "dismiss_picker", "Cancel"),
    ]

    DEFAULT_CSS = """
    ThemePickerScreen {
        align: center middle;
    }

    ThemePickerScreen > Vertical {
        background: $surface;
        border: thick $accent;
        padding: 1 2;
        width: 60%;
        max-width: 66;
        height: auto;
        max-height: 16;
    }

    ThemePickerScreen Label.title {
        text-style: bold;
        padding: 0 0 1 0;
    }

    ThemePickerScreen Label.hint {
        color: $text-muted;
        padding: 1 0 0 0;
    }

    ThemePickerScreen OptionList {
        height: auto;
    }
    """

    def __init__(self, active: str = "ithildin") -> None:
        super().__init__()
        self._active = active

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Pick a palette", classes="title")
            options = []
            for name, desc in _PALETTES:
                marker = " ← active" if name == self._active else ""
                options.append(Option(f"{name} — {desc}{marker}", id=name))
            yield OptionList(*options, id="theme-picker-list")
            yield Label(
                "Enter to switch · Ctrl+T toggles light/dark · Esc to cancel",
                classes="hint",
            )

    def on_mount(self) -> None:
        try:
            self.query_one("#theme-picker-list", OptionList).focus()
        except Exception:  # noqa: BLE001
            pass

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss(event.option.id)

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
