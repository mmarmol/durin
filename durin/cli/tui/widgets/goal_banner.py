"""GoalBanner — sticky one-line banner showing the active goal.

Hidden when there is no active goal; shown with an accent tint while a goal is
running. Fed from goal-state events the app receives on the outbound bus.
"""

from __future__ import annotations

from textual.widgets import Static

__all__ = ["GoalBanner"]


class GoalBanner(Static):
    DEFAULT_CSS = """
    GoalBanner {
        height: 1; display: none; padding: 0 1;
        background: $accent 15%; color: $text;
    }
    GoalBanner.--shown { display: block; }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._objective: str | None = None
        self._progress: str = ""

    @property
    def is_shown(self) -> bool:
        return self.has_class("--shown")

    def render_text(self) -> str:
        suffix = f"  [dim]{self._progress}[/]" if self._progress else ""
        return f"[$accent]◎ Goal[/] {self._objective}{suffix}"

    def set_goal(self, objective: str | None, progress: str = "") -> None:
        self._objective = (objective or "").strip() or None
        self._progress = progress
        if self._objective:
            self.add_class("--shown")
            self.update(self.render_text())
        else:
            self.remove_class("--shown")
            self.update("")
