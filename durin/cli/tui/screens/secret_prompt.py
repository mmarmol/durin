"""SecretPromptScreen — masked modal for storing a requested credential.

Opened from a ``request_secret`` tool bubble. The user types the secret
value into a password-masked field; on submit it is written straight to
the :class:`~durin.security.secrets.SecretStore`. The value never enters
the chat, the agent context, or a tool result — only the fact that the
secret now exists is later reported back to the agent.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label

__all__ = ["SecretPromptScreen"]


class SecretPromptScreen(ModalScreen[bool]):
    """Masked prompt that stores a credential. Returns True once stored."""

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

    def __init__(self, *, name: str, service: str, purpose: str = "") -> None:
        super().__init__()
        self._name = name
        self._service = service
        self._purpose = purpose

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"🔑 Provide secret: {self._name}", classes="title")
            yield Label(f"service: {self._service}", classes="meta")
            if self._purpose:
                yield Label(self._purpose, classes="meta")
            yield Input(
                password=True,
                placeholder="paste the secret value, then ⏎",
                id="secret-input",
            )
            yield Label(
                "Stored straight to durin's secret store — the value never "
                "reaches the model or the chat.",
                classes="hint",
            )
            yield Label("", id="secret-error", classes="error")

    def on_mount(self) -> None:
        self.query_one("#secret-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save(event.value)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _save(self, value: str) -> None:
        value = (value or "").strip()
        if not value:
            self._show_error("The value is required.")
            return
        try:
            from durin.service.secrets import SecretsService

            SecretsService().store_entry(
                name=self._name,
                value=value,
                service=self._service,
                scope=["exec"],
                origin="tui",
            )
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"Could not store the secret: {exc}")
            return
        self.dismiss(True)

    def _show_error(self, message: str) -> None:
        try:
            self.query_one("#secret-error", Label).update(message)
        except Exception:  # noqa: BLE001
            pass
