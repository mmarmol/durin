"""SecretPromptScreen — masked modal for storing a requested credential.

Opened from a ``request_secret`` tool bubble. The user types the secret
value into a password-masked field; on submit it is written straight to
the :class:`~durin.security.secrets.SecretStore`. In replace mode
(``update=True``) only the value of the existing secret is rotated —
metadata and scope are preserved. The value never enters the chat, the
agent context, or a tool result — only the fact that the secret now
exists is later reported back to the agent.

Dismissing returns the stored entry's metadata rather than a bare flag:
replace mode preserves whatever scope the entry already had, so a caller
that assumed one would describe the credential wrongly to the agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label

if TYPE_CHECKING:
    from durin.service.secrets import SecretItem

__all__ = ["SecretPromptScreen"]


class SecretPromptScreen(ModalScreen["SecretItem | None"]):
    """Masked prompt that stores a credential.

    Returns the stored entry's metadata (never the value), or None when
    cancelled.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    SecretPromptScreen {
        align: center middle;
    }
    SecretPromptScreen > Vertical {
        background: $surface;
        border: thick $accent;
        padding: 1 2;
        width: 70%;
        max-width: 72;
        height: auto;
    }
    SecretPromptScreen Label.title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    SecretPromptScreen Label.meta {
        color: $text-muted;
    }
    SecretPromptScreen Input {
        margin: 1 0 0 0;
    }
    SecretPromptScreen Label.hint {
        color: $text-muted;
        text-style: italic;
        padding: 1 0 0 0;
    }
    SecretPromptScreen Label.error {
        color: $error;
        padding: 1 0 0 0;
    }
    """

    def __init__(
        self, *, name: str, service: str, purpose: str = "", update: bool = False
    ) -> None:
        super().__init__()
        self._name = name
        self._service = service
        self._purpose = purpose
        self._update = update

    def compose(self) -> ComposeResult:
        title = (
            f"🔁 Replace secret: {self._name}"
            if self._update
            else f"🔑 Provide secret: {self._name}"
        )
        placeholder = (
            "paste the NEW value, then ⏎"
            if self._update
            else "paste the secret value, then ⏎"
        )
        with Vertical():
            yield Label(title, classes="title")
            yield Label(f"service: {self._service}", classes="meta")
            if self._purpose:
                yield Label(self._purpose, classes="meta")
            yield Input(
                password=True,
                placeholder=placeholder,
                id="secret-input",
            )
            yield Label(
                "Stored straight to durin's secret store — the value never "
                "reaches the model or the chat.",
                classes="hint",
            )
            if self._update:
                yield Label(
                    "Only the value is replaced — service, scope and "
                    "description stay unchanged.",
                    classes="hint",
                )
            yield Label("", id="secret-error", classes="error")

    def on_mount(self) -> None:
        self.query_one("#secret-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self, value: str) -> None:
        value = (value or "").strip()
        if not value:
            self._show_error("The value is required.")
            return
        stored: SecretItem
        try:
            from durin.service.secrets import SecretsService

            if self._update:
                stored = SecretsService().store_entry(
                    name=self._name, value=value, rotate=True,
                )
            else:
                stored = SecretsService().store_entry(
                    name=self._name,
                    value=value,
                    service=self._service,
                    scope=["exec"],
                    origin="tui",
                )
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"Could not store the secret: {exc}")
            return
        self.dismiss(stored)

    def _show_error(self, message: str) -> None:
        try:
            self.query_one("#secret-error", Label).update(message)
        except Exception:  # noqa: BLE001
            pass
