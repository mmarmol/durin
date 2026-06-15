"""WorkingIndicator — small animated 'thinking…' line between submit + reply.

Shown the moment the user submits a turn and hidden as soon as the first
reasoning or content delta arrives. Matches pi-agent's `Working…`
behaviour: enough movement to confirm the agent is busy, no fanfare.
"""

from __future__ import annotations

from textual.widgets import Static

__all__ = ["WorkingIndicator"]


class WorkingIndicator(Static):
    """One-line animated indicator: spinner + label."""

    # Braille spinner — universally supported, no extra fonts needed.
    _FRAMES: tuple[str, ...] = (
        "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
    )

    DEFAULT_CSS = """
    WorkingIndicator {
        height: 1;
        padding: 0 2;
        margin: 0 2;
        color: $text-muted;
        text-style: italic;
    }
    """

    def __init__(self, label: str = "thinking…") -> None:
        super().__init__("")
        self._label = label
        self._frame = 0

    def on_mount(self) -> None:
        # 80ms is the Goldilocks tempo — fast enough to feel active,
        # slow enough not to thrash the terminal.
        self._timer = self.set_interval(0.08, self._tick)
        self._render_frame()

    def on_unmount(self) -> None:
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.stop()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(self._FRAMES)
        self._render_frame()

    def _render_frame(self) -> None:
        self.update(f"{self._FRAMES[self._frame]} {self._label}  ·  Esc to stop")
