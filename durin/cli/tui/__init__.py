"""Textual-based TUI for durin (Phase D5, opt-in via `durin agent --tui`).

Migration plan + feature-parity inventory live in
``docs/10_textual_migration.md``. The package is intentionally small in
D5.1 — just the App entry point with a placeholder — so the legacy
``durin/cli/commands.py`` path stays the production CLI until D5
reaches parity (D5.12) and gets real use.
"""

from durin.cli.tui.app import DurinApp, run_durin_tui

__all__ = ["DurinApp", "run_durin_tui"]
