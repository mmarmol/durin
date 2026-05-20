"""SessionPickerScreen — modal that lets the user pick a saved session.

Opened via the `/sessions` slash command (when typed with no
filter argument) and dismissed with Esc. Selecting an option
returns the session key to the App, which then publishes
``/resume <key>`` through the bus so the existing CommandRouter
handles the actual switch.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

__all__ = ["SessionEntry", "SessionPickerScreen"]


@dataclass(frozen=True)
class SessionEntry:
    key: str
    display_name: str
    msg_count: int
    updated_at: str


class SessionPickerScreen(ModalScreen[str | None]):
    """Modal that returns the selected session key, or ``None`` on cancel."""

    BINDINGS = [
        Binding("escape", "dismiss_picker", "Cancel"),
    ]

    DEFAULT_CSS = """
    SessionPickerScreen {
        align: center middle;
    }

    SessionPickerScreen > Vertical {
        background: $surface;
        border: thick $accent;
        padding: 1 2;
        width: 80%;
        max-width: 100;
        height: 70%;
        max-height: 30;
    }

    SessionPickerScreen Label.title {
        text-style: bold;
        padding: 0 0 1 0;
    }

    SessionPickerScreen Label.hint {
        color: $text-muted;
        padding: 1 0 0 0;
    }

    SessionPickerScreen OptionList {
        height: 1fr;
    }
    """

    def __init__(self, sessions: list[SessionEntry], current_key: str = "") -> None:
        super().__init__()
        self._sessions = sessions
        self._current_key = current_key

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Pick a session", classes="title")
            options = []
            for s in self._sessions:
                marker = " ← current" if s.key == self._current_key else ""
                name_part = f" — {s.display_name}" if s.display_name else ""
                when = s.updated_at[:16].replace("T", " ") if s.updated_at else ""
                label = f"{s.key}{name_part} · {s.msg_count} msgs · {when}{marker}"
                options.append(Option(label, id=s.key))
            yield OptionList(*options, id="session-picker-list")
            yield Label("Enter to switch · Esc to cancel", classes="hint")

    def on_mount(self) -> None:
        try:
            self.query_one("#session-picker-list", OptionList).focus()
        except Exception:  # noqa: BLE001
            pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
