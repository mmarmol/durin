"""Textual-based TUI for durin (opt-in via `durin agent --tui`).

Entry point for the Textual-based interface. The legacy
``durin/cli/commands.py`` path stays the production CLI until this
implementation reaches full feature parity.
"""

from durin.cli.tui.app import DurinApp, run_durin_tui

__all__ = ["DurinApp", "run_durin_tui"]
