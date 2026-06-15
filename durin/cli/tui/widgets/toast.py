"""Toast notifications — transient feedback messages.

Displays a small notification at the top-right of the TUI that auto-dismisses
after a short delay. Useful for copy confirmations, save success, etc.

API: ``app.toast("Copied!", level="success")``
"""

from __future__ import annotations

from typing import Literal

from textual.widgets import Static

__all__ = ["ToastNotification"]

ToastLevel = Literal["info", "success", "warning", "error"]

_ICONS: dict[ToastLevel, str] = {
    "info": "ℹ",
    "success": "✓",
    "warning": "⚠",
    "error": "✗",
}


class ToastNotification(Static):
    """One toast notification — auto-dismisses after ``duration`` seconds."""

    DEFAULT_CSS = """
    ToastNotification {
        layer: _toast;
        dock: top;
        offset: 0 0;
        align: right top;
        width: auto;
        max-width: 60;
        min-width: 20;
        height: 1;
        padding: 0 2;
        margin: 0 1;
        background: $surface-lighten-2;
        border: round $primary 50%;
        color: $text;
        text-style: bold;
    }
    ToastNotification.-success {
        border: round $success 50%;
        color: $success;
    }
    ToastNotification.-warning {
        border: round $warning 50%;
        color: $warning;
    }
    ToastNotification.-error {
        border: round $error 50%;
        color: $error;
    }
    """

    def __init__(self, message: str, level: ToastLevel = "info", duration: float = 2.0) -> None:
        icon = _ICONS.get(level, "")
        text = f"{icon} {message}" if icon else message
        super().__init__(text, classes=f"-{level}")
        self._duration = duration
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_timer(self._duration, self._dismiss)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _dismiss(self) -> None:
        """Remove self from the parent."""
        try:
            self.remove()
        except Exception:  # noqa: BLE001
            pass
