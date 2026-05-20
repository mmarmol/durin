"""InputArea — the typing surface at the bottom of the chat.

Thin subclass of :class:`textual.widgets.Input` whose sole purpose
today is to provide a stable type the App can target, plus the
``/``-prefix slash-command suggester (D5.4).

The parent ``Input.Submitted`` message is the canonical event — the
App listens for it directly via ``on_input_submitted``.

Subsequent sub-tasks layer behaviour on top of this class:

- D5.6 — drag-and-drop pre-processing inside ``on_input_submitted``.
- D5.7 — Esc / Ctrl key bindings.
- D5.8 — ``@file`` completion via a richer ``Suggester``.
"""

from __future__ import annotations

from textual.suggester import Suggester
from textual.widgets import Input

from durin.command.builtin import BUILTIN_COMMAND_SPECS

__all__ = ["InputArea", "SlashCommandSuggester"]


class SlashCommandSuggester(Suggester):
    """Suggest a known slash command when the buffer starts with ``/``.

    Returns the *first* matching command in declaration order. Pressing
    Right Arrow or End accepts the suggestion. Bug-for-bug-equivalent
    to the legacy CLI's ``BUILTIN_COMMAND_SPECS`` palette.
    """

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        # Snapshot at construction; the palette is module-level.
        self._commands: list[str] = [spec.command for spec in BUILTIN_COMMAND_SPECS]

    async def get_suggestion(self, value: str) -> str | None:
        if not value.startswith("/"):
            return None
        if not value or value == "/":
            return None
        for cmd in self._commands:
            if cmd != value and cmd.lower().startswith(value.lower()):
                return cmd
        return None


class InputArea(Input):
    """User input widget.

    Wires ``SlashCommandSuggester`` by default so ``/`` triggers
    in-place autocomplete. The suggester is replaceable via the
    ``suggester`` constructor arg (D5.8 swaps it for a multi-mode
    suggester that also handles ``@file`` references).
    """

    DEFAULT_CSS = """
    InputArea {
        height: 3;
        margin: 0 0 1 0;
    }
    """

    def __init__(
        self,
        *,
        placeholder: str = "Type a message …",
        suggester: Suggester | None = None,
    ) -> None:
        super().__init__(
            placeholder=placeholder,
            suggester=suggester or SlashCommandSuggester(),
        )
