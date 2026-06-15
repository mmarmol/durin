"""Command registry — aggregates all commands available in the TUI.

Used by the command palette (Ctrl+P) to present a unified, fuzzy-searchable
list of slash commands and quick actions.

Each entry is a :class:`CommandEntry` with:

- ``id`` — unique key (e.g. ``cmd:/model``, ``act:open_model_picker``)
- ``label`` — display text (e.g. ``/model — switch model``)
- ``kind`` — ``"command"`` or ``"action"``
- ``description`` — short help text
- ``shortcut`` — keybinding hint (e.g. ``Ctrl+L``) or empty
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CommandEntry", "build_command_entries"]


@dataclass(frozen=True, slots=True)
class CommandEntry:
    """A single command or action shown in the palette."""

    id: str
    label: str
    kind: str  # "command" or "action"
    description: str
    shortcut: str = ""


# Static action list — these map to ``DurinApp`` methods/behaviors.
_TUI_ACTIONS: list[CommandEntry] = [
    CommandEntry(
        id="act:open_model_picker",
        label="Switch model",
        kind="action",
        description="Pick from recent, configured, or suggested models",
        shortcut="Ctrl+L",
    ),
    CommandEntry(
        id="act:open_theme_picker",
        label="Switch theme",
        kind="action",
        description="Change the colour palette",
        shortcut="Ctrl+T",
    ),
    CommandEntry(
        id="act:open_session_picker",
        label="Switch session",
        kind="action",
        description="Resume a different conversation",
    ),
    CommandEntry(
        id="act:copy_last",
        label="Copy last reply",
        kind="action",
        description="Copy the last assistant message to clipboard",
        shortcut="Ctrl+Y",
    ),
    CommandEntry(
        id="act:toggle_dark",
        label="Toggle dark/light",
        kind="action",
        description="Flip between dark and light mode",
        shortcut="Ctrl+T",
    ),
    CommandEntry(
        id="act:abort",
        label="Stop generation",
        kind="action",
        description="Cancel the in-flight agent turn",
        shortcut="Esc",
    ),
    CommandEntry(
        id="act:open_command_palette",
        label="Command palette",
        kind="action",
        description="Search and run any command",
        shortcut="Ctrl+P",
    ),
    CommandEntry(
        id="act:toggle_sidebar",
        label="Toggle sidebar",
        kind="action",
        description="Show/hide Todos, Files, MCP panel",
        shortcut="Ctrl+B",
    ),
    CommandEntry(
        id="act:quit",
        label="Quit durin",
        kind="action",
        description="Exit the TUI",
        shortcut="Ctrl+Q",
    ),
]


# Slash commands with human-readable descriptions.
_SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/new", "Start a fresh conversation"),
    ("/status", "Show agent and session status"),
    ("/model", "Switch or inspect the active model"),
    ("/sessions", "List saved sessions"),
    ("/resume", "Resume a session by key"),
    ("/compact", "Compact the conversation context"),
    ("/history", "Browse recent goal/task history"),
    ("/goal", "Set or inspect the active goal"),
    ("/plan", "Switch to plan mode"),
    ("/build", "Switch to build mode"),
    ("/mode", "Show or change the agent mode"),
    ("/copy", "Copy the last assistant reply"),
    ("/name", "Rename the current session"),
    ("/memory", "Search agent memory"),
    ("/skills", "List or manage skills"),
    ("/remember", "Save a memory fragment"),
    ("/forget", "Remove a memory fragment"),
    ("/sources", "Show sources for the last reply"),
    ("/audit", "Show the audit trail"),
    ("/why", "Explain a past decision"),
    ("/help", "Show full command help"),
    ("/pairing", "Pair a messaging channel"),
    ("/hotkeys", "List all keyboard shortcuts"),
    ("/dream", "Trigger dream consolidation"),
    ("/dream-log", "View dream log entries"),
    ("/dream-restore", "Restore a dream snapshot"),
    ("/stop", "Stop the current turn (same as Esc)"),
    ("/restart", "Restart the agent loop"),
]


def build_command_entries() -> list[CommandEntry]:
    """Build the full list of palette entries.

    Returns slash commands first, then TUI actions.
    """
    commands = [
        CommandEntry(
            id=f"cmd:{name}",
            label=f"{name} — {desc}",
            kind="command",
            description=desc,
        )
        for name, desc in _SLASH_COMMANDS
    ]
    return commands + list(_TUI_ACTIONS)
