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

__all__ = ["AtFileSuggester", "InputArea", "MultiModeSuggester", "SlashCommandSuggester"]


# Commands handled locally by the TUI app (not dispatched through the shared
# command registry), added on top of the derived registry list below.
_TUI_LOCAL_COMMANDS: tuple[str, ...] = ("/theme", "/voice")


def _slash_command_names() -> list[str]:
    # Imported lazily, matching command_registry.py's derivation — keeps
    # this widget module decoupled from the bus/router import chain that
    # durin.command.builtin pulls in at module load time.
    from durin.command.builtin import specs_for_surface

    names = [spec.command for spec in specs_for_surface("tui")]
    for extra in _TUI_LOCAL_COMMANDS:
        if extra not in names:
            names.append(extra)
    return names


# Known subcommand sets for slash commands that take a verb as their first
# argument. Used by `SlashCommandSuggester` to extend the autocomplete to
# the second token of e.g. `/memory list`, `/mode plan`, `/pairing approve`.
#
# Keep this hand-curated — derived statically from `BUILTIN_COMMAND_SPECS`
# arg_hints, but typed out so we don't parse hint strings at runtime.
_SLASH_SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    "/memory": ("list", "show", "search", "drill", "ingest"),
    "/mode": ("build", "plan"),
    "/pairing": ("list", "approve", "deny", "revoke"),
    "/sources": ("list", "ingest"),
    "/audit": (),
    "/why": (),
}


class SlashCommandSuggester(Suggester):
    """Suggest a known slash command (or subcommand) as the user types.

    Two-level autocomplete:
    1. Buffer starts with ``/foo`` (no space) → suggest a matching top-level
       slash command, e.g. ``/me`` → ``/memory``.
    2. Buffer is ``/cmd `` or ``/cmd partial`` → suggest a matching
       subcommand for that command, e.g. ``/memory l`` → ``/memory list``.

    Pressing Right Arrow / End / Tab accepts the suggestion. The legacy
    behaviour for the first-level case is unchanged; the second level is
    additive.
    """

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=False)
        self._commands: list[str] = _slash_command_names()

    async def get_suggestion(self, value: str) -> str | None:
        if not value.startswith("/") or value == "/":
            return None
        # Second-level: a space inside `/cmd …` means we're completing a
        # subcommand for ``cmd``.
        if " " in value:
            head, _, partial = value.partition(" ")
            subcommands = _SLASH_SUBCOMMANDS.get(head.lower())
            if not subcommands:
                return None
            partial_low = partial.lower()
            for sub in subcommands:
                if sub.lower().startswith(partial_low) and sub != partial:
                    return f"{head} {sub}"
            return None
        # First-level: complete the command name itself.
        for cmd in self._commands:
            if cmd != value and cmd.lower().startswith(value.lower()):
                return cmd
        return None

    def candidates(self, value: str) -> list[str]:
        """Return *all* matches for ``value`` (not just the first).

        Used by the multi-option dropdown below the input. Returns an
        empty list when there's no slash prefix to autocomplete.
        """
        if not value.startswith("/") or value == "/":
            return []
        if " " in value:
            head, _, partial = value.partition(" ")
            subcommands = _SLASH_SUBCOMMANDS.get(head.lower())
            if not subcommands:
                return []
            partial_low = partial.lower()
            return [
                f"{head} {sub}"
                for sub in subcommands
                if sub.lower().startswith(partial_low)
            ]
        return [cmd for cmd in self._commands if cmd.lower().startswith(value.lower())]


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

    def candidates(self, value: str) -> list[str]:
        """Return all matching candidates for the multi-option dropdown."""
        if value.startswith("/"):
            return self._slash.candidates(value)
        return []


class InputArea(Input):
    """User input widget.

    Default suggester is :class:`MultiModeSuggester` when a workspace
    is supplied (slash + @file), otherwise :class:`SlashCommandSuggester`.

    D3.1 — Alt+Enter inserts a literal newline into the value so the
    user can author multi-line messages. The single-line display will
    look truncated, but the submitted value preserves the newlines.
    """

    BINDINGS = [
        ("alt+enter", "insert_newline", "Newline"),
        ("ctrl+j", "insert_newline", "Newline"),  # most terminals send ^J on Ctrl+Enter
        ("tab", "accept_suggestion", "Complete"),
        ("up", "history_prev", "History ↑"),
        ("down", "history_next", "History ↓"),
    ]

    DEFAULT_CSS = """
    /* Minimal styling. We only override:
       - top border: thin line to visually separate input from chat
       - height: room for top border + input line + breathing room
       - background: surface so the input is visibly distinct from the
         transparent chat area
       Everything else (cursor, placeholder, value color) inherits from
       Textual's Input defaults — that's the safest path while we're
       still hunting the "click makes text invisible" bug. */
    InputArea {
        height: 3;
        border-top: hkey #555555;
        border-left: none;
        border-right: none;
        border-bottom: none;
        background: $surface;
        padding: 0 2;
        margin: 0;
    }
    InputArea:focus {
        border-top: hkey #8abeb7;
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
        self._history: list[str] = []
        self._history_index: int = -1  # -1 = not browsing

    def load_history(self, prompts: list[str]) -> None:
        """Load prompt history for Up/Down recall."""
        self._history = prompts
        self._history_index = -1

    def action_history_prev(self) -> None:
        """Up arrow: recall previous prompt from history."""
        if not self._history:
            return
        if self._history_index == -1:
            # Start browsing from the last entry
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return
        self.value = self._history[self._history_index]
        self.cursor_position = len(self.value)

    def action_history_next(self) -> None:
        """Down arrow: recall next prompt (or clear if at end)."""
        if self._history_index == -1:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.value = self._history[self._history_index]
        else:
            # Reached the end — clear and exit browsing mode
            self._history_index = -1
            self.value = ""
        self.cursor_position = len(self.value)

    def action_insert_newline(self) -> None:
        """Inject ``\\n`` at the cursor; preserved in the submitted value."""
        pos = self.cursor_position
        self.value = self.value[:pos] + "\n" + self.value[pos:]
        self.cursor_position = pos + 1

    async def action_accept_suggestion(self) -> None:
        """Tab: accept the current inline suggestion (if any).

        Mirrors what Right Arrow / End does in Textual's Input, but bound
        to Tab so the keyboard flow matches what shell users expect.
        Falls through silently when there's no suggestion — Tab on an
        empty buffer is a no-op rather than focusing the next widget.
        """
        suggester = self.suggester
        if suggester is None:
            return
        suggestion = await suggester.get_suggestion(self.value)
        if suggestion and suggestion != self.value:
            self.value = suggestion
            self.cursor_position = len(self.value)
