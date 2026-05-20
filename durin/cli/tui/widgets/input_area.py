"""InputArea — the typing surface at the bottom of the chat.

Thin subclass of :class:`textual.widgets.Input` that wires:

- D5.4 SlashCommandSuggester (``/<prefix>`` → known command).
- D5.8 AtFileSuggester      (``@<prefix>`` → workspace file).
- D5.8 MultiModeSuggester   dispatches between the two.

The parent ``Input.Submitted`` message is the canonical event — the
App listens for it directly via ``on_input_submitted``.

Subsequent sub-tasks layer behaviour on top of this class:

- D5.6 — drag-and-drop pre-processing inside ``on_input_submitted``.
- D5.7 — Esc / Ctrl key bindings.
"""

from __future__ import annotations

from pathlib import Path

from textual.suggester import Suggester
from textual.widgets import Input

from durin.command.builtin import BUILTIN_COMMAND_SPECS

__all__ = ["AtFileSuggester", "InputArea", "MultiModeSuggester", "SlashCommandSuggester"]


class SlashCommandSuggester(Suggester):
    """Suggest a known slash command when the buffer starts with ``/``.

    Returns the *first* matching command in declaration order. Pressing
    Right Arrow or End accepts the suggestion. Bug-for-bug-equivalent
    to the legacy CLI's ``BUILTIN_COMMAND_SPECS`` palette.
    """

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
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


class AtFileSuggester(Suggester):
    """Suggest a workspace-relative file after ``@<prefix>`` (D5.8).

    Mirrors :class:`durin.cli.completers.FileReferenceCompleter`'s scan
    rules (excludes ``.git`` / ``__pycache__`` / ``.venv`` etc., cached
    walk capped at ``MAX_FILES``) so behaviour stays consistent across
    the two CLI surfaces.
    """

    MAX_FILES = 1000

    def __init__(self, workspace: Path) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self._workspace = workspace.expanduser().resolve()
        self._cached: list[str] | None = None

    def invalidate(self) -> None:
        self._cached = None

    def _scan_files(self) -> list[str]:
        if self._cached is not None:
            return self._cached
        from durin.cli.completers import FileReferenceCompleter

        self._cached = FileReferenceCompleter(self._workspace)._scan_files()
        return self._cached

    async def get_suggestion(self, value: str) -> str | None:
        if "@" not in value:
            return None
        at_idx = value.rfind("@")
        if at_idx > 0 and not value[at_idx - 1].isspace():
            return None
        prefix = value[at_idx + 1 :]
        if any(c.isspace() for c in prefix):
            return None
        if not prefix:
            return None
        prefix_low = prefix.lower()
        for path in self._scan_files():
            if prefix_low in path.lower():
                return value[: at_idx + 1] + path
        return None


class MultiModeSuggester(Suggester):
    """Dispatch suggester: slash commands when ``/`` prefix, files after ``@``."""

    def __init__(self, *, workspace: Path | None) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self._slash = SlashCommandSuggester()
        self._at = AtFileSuggester(workspace) if workspace is not None else None

    async def get_suggestion(self, value: str) -> str | None:
        if value.startswith("/"):
            return await self._slash.get_suggestion(value)
        if self._at is not None and "@" in value:
            return await self._at.get_suggestion(value)
        return None


class InputArea(Input):
    """User input widget.

    Default suggester is :class:`MultiModeSuggester` when a workspace
    is supplied (slash + @file), otherwise :class:`SlashCommandSuggester`.
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
        workspace: Path | None = None,
    ) -> None:
        default_suggester = (
            MultiModeSuggester(workspace=workspace)
            if workspace is not None
            else SlashCommandSuggester()
        )
        super().__init__(
            placeholder=placeholder,
            suggester=suggester or default_suggester,
        )
