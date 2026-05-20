"""HeaderBar — top-of-screen chrome showing identity + workspace + model."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static

__all__ = ["HeaderBar"]


class HeaderBar(Horizontal):
    """A one-line header: ``durin · <workspace> · <model> (<preset>)``."""

    DEFAULT_CSS = """
    HeaderBar {
        height: 1;
        background: $surface;
        color: $text;
    }
    HeaderBar Static {
        padding: 0 1;
    }
    HeaderBar .brand {
        color: $accent;
        text-style: bold;
    }
    HeaderBar .meta {
        color: $text-muted;
    }
    """

    workspace_path: reactive[str] = reactive("")
    model: reactive[str] = reactive("")
    preset: reactive[str] = reactive("default")

    def __init__(
        self,
        *,
        workspace_path: str | Path = "",
        model: str = "",
        preset: str = "default",
    ) -> None:
        super().__init__()
        self.workspace_path = str(workspace_path)
        self.model = model or "?"
        self.preset = preset or "default"

    def compose(self) -> ComposeResult:
        yield Static("durin", classes="brand")
        yield Static(self._meta_line(), id="header-meta", classes="meta")

    def _meta_line(self) -> str:
        ws = self.workspace_path or "?"
        # Collapse $HOME → ~ for visual compactness.
        try:
            home = str(Path.home())
            if ws.startswith(home):
                ws = "~" + ws[len(home):]
        except Exception:
            pass
        return f"· {ws} · {self.model} ({self.preset})"

    def watch_workspace_path(self, _old: str, _new: str) -> None:
        self._refresh()

    def watch_model(self, _old: str, _new: str) -> None:
        self._refresh()

    def watch_preset(self, _old: str, _new: str) -> None:
        self._refresh()

    def _refresh(self) -> None:
        try:
            self.query_one("#header-meta", Static).update(self._meta_line())
        except Exception:
            # Pre-mount; reactive defaults handle initial render.
            pass
