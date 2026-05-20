"""InputArea — the typing surface at the bottom of the chat.

This is a thin subclass of :class:`textual.widgets.Input` whose sole
purpose today is to provide a stable type the App can target. The
parent ``Input.Submitted`` message is the canonical event — the App
listens for it directly via ``on_input_submitted``.

Subsequent sub-tasks layer behaviour on top of this class:

- D5.6 — drag-and-drop pre-processing inside ``on_input_submitted``.
- D5.7 — Esc / Ctrl key bindings.
- D5.8 — ``@file`` completion via a ``Suggester``.
"""

from __future__ import annotations

from textual.widgets import Input

__all__ = ["InputArea"]


class InputArea(Input):
    """Subclass marker — same behaviour as ``Input`` for now."""

    DEFAULT_CSS = """
    InputArea {
        height: 3;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, *, placeholder: str = "Type a message …") -> None:
        super().__init__(placeholder=placeholder)
