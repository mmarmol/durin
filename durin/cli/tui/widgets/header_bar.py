"""HeaderBar — top-of-screen title, modelled on pi-agent's startup chrome.

Pi shows a compact single-line title: ``PI - MANIPULATE THE WEBSITE •``
with a status dot on the right. The brand on the left is the literal app
name; the middle conveys the active task / session; the dot signals
status (busy / idle).

We mirror that shape: ``durin - <session label> •``.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static

__all__ = ["HeaderBar"]


class HeaderBar(Horizontal):
    """One-line header: ``durin - <session label> <status-dot>``."""

    DEFAULT_CSS = """
    HeaderBar {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 2;
    }
    HeaderBar > #header-title {
        width: 1fr;
        color: $text;
    }
    HeaderBar > #header-status {
        width: auto;
        color: $accent;
    }
    HeaderBar .brand {
        color: $accent;
        text-style: bold;
    }
    """

    session_label: reactive[str] = reactive("")
    session_meta: reactive[str] = reactive("")  # e.g. "47 msgs · 12h ago"
    is_busy: reactive[bool] = reactive(False)

    def __init__(
        self,
        *,
        session_label: str = "",
        session_meta: str = "",
    ) -> None:
        super().__init__()
        self.session_label = session_label or "scratch"
        self.session_meta = session_meta

    def compose(self) -> ComposeResult:
        yield Static(self._title_text(), id="header-title", markup=True)
        yield Static(self._status_glyph(), id="header-status")

    def _title_text(self) -> str:
        label = self.session_label or "scratch"
        meta = self.session_meta
        if meta:
            return f"[bold $accent]durin[/] · {label}  [dim]· {meta}[/dim]"
        return f"[bold $accent]durin[/] · {label}"

    def _status_glyph(self) -> str:
        # pi uses `•` for idle and dims/animates while busy. Plain dot for now.
        return "●" if self.is_busy else "○"

    def watch_session_label(self, _old: str, _new: str) -> None:
        self._refresh()

    def watch_session_meta(self, _old: str, _new: str) -> None:
        self._refresh()

    def watch_is_busy(self, _old: bool, _new: bool) -> None:
        self._refresh()

    def _refresh(self) -> None:
        try:
            self.query_one("#header-title", Static).update(self._title_text())
            self.query_one("#header-status", Static).update(self._status_glyph())
        except Exception:
            pass
