"""Clipboard helpers shared between the slash-command CLI and the TUI."""

from __future__ import annotations

import subprocess

__all__ = ["copy_text", "NoClipboardError"]


class NoClipboardError(RuntimeError):
    """Raised when no system clipboard tool is available."""


def copy_text(text: str) -> str:
    """Copy ``text`` to the system clipboard. Returns the tool name used.

    Tries the platform's native CLI tool (pbcopy / xclip / wl-copy / clip).
    Raises :class:`NoClipboardError` if none are installed.
    """
    candidates = [
        ("pbcopy", ["pbcopy"]),                              # macOS
        ("xclip", ["xclip", "-selection", "clipboard"]),     # Linux X
        ("wl-copy", ["wl-copy"]),                            # Linux Wayland
        ("clip", ["clip"]),                                  # Windows
    ]
    for name, cmd in candidates:
        try:
            subprocess.run(cmd, input=text, text=True, check=True, capture_output=True)
            return name
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    raise NoClipboardError(
        "no clipboard tool found — install pbcopy (macOS), xclip / wl-copy "
        "(Linux), or run on Windows where `clip` is built in"
    )
