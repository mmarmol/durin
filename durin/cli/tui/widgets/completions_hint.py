"""CompletionsHint — one-line dropdown listing all candidates for the
current input. Sits between the chat view and the input so the user
sees the menu of possibilities as they type.

Mounted in :class:`durin.cli.tui.app.DurinApp`. Updates from the app's
``on_input_changed`` handler, not from this widget directly — keeps the
inter-widget plumbing in one place.
"""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

__all__ = ["CompletionsHint"]


class CompletionsHint(Static):
    """Dim one-line list of `[opt1] [opt2] [opt3]` candidate completions.

    Hidden (zero height) when there's nothing useful to show. The first
    candidate is highlighted — that's the one Tab / → Arrow will accept.
    """

    DEFAULT_CSS = """
    CompletionsHint {
        height: auto;
        max-height: 1;
        padding: 0 2;
        background: $surface;
        color: $text-muted;
    }
    CompletionsHint.hidden {
        display: none;
    }
    """

    text: reactive[str] = reactive("")

    def __init__(self) -> None:
        super().__init__("", id="completions-hint")
        self.add_class("hidden")

    def show_candidates(self, candidates: list[str]) -> None:
        """Render up to ~7 candidates, with the first one highlighted."""
        if not candidates:
            self.set_class(True, "hidden")
            self.update("")
            return
        # Limit + render. First match is bold/accent — the "default" Tab pick.
        shown = candidates[:7]
        more = len(candidates) - len(shown)
        parts = []
        for i, c in enumerate(shown):
            if i == 0:
                parts.append(f"[bold $accent]{c}[/bold $accent]")
            else:
                parts.append(c)
        line = "  ".join(parts)
        if more > 0:
            line += f"  [dim](+{more} more)[/dim]"
        self.set_class(False, "hidden")
        self.update(line)

    def clear(self) -> None:
        self.show_candidates([])
