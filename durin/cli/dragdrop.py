"""Drag-and-drop input pre-processor for the interactive CLI.

When the user drags a file onto iTerm2, Terminal.app, Wezterm, kitty,
or most modern terminals, the absolute path is typed into stdin as if
the user had pasted it. This module scans the user's typed message for
path-like substrings, classifies them by extension, and:

- For images and audio: copies the file to ``<workspace>/.media/<sha>.<ext>``
  (content-hash idempotent) and rewrites the path in the message to the
  stable copy. The agent's vision / audio aux models then operate on a
  workspace-local artifact that survives even if the user moves the
  original.
- For documents (markdown, text, PDF): the path is left untouched in
  the message so the agent's ``read_file`` tool can pick it up.

The copied paths are returned alongside the cleaned text so the CLI
can populate ``InboundMessage.media`` — the loop's existing media
plumbing (``durin/agent/loop.py``) already consumes that field.

Python 3.11+ for ``Path.is_relative_to``.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

__all__ = ["process_dragged_paths"]


# Absolute (POSIX) or home-relative path; bash-style escaped spaces tolerated.
# Example matches:
#   /Users/me/Pictures/foo.png
#   ~/Documents/notes.md
#   /tmp/with\ space.png
_PATH_RE = re.compile(
    r"""
    (?:(?<=\s)|^)                     # word boundary or start of string
    (
      ~?                              # optional leading ~
      /                               # absolute root
      (?:                             # path segment(s)
        [^\s\\"']+                    # plain segment chars
        |
        \\\s                          # or escaped whitespace
      )+
    )
    """,
    re.VERBOSE,
)


_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus"})
_COPIED_EXTS = _IMAGE_EXTS | _AUDIO_EXTS


def process_dragged_paths(text: str, workspace: Path) -> tuple[str, list[str]]:
    """Scan ``text`` for absolute file paths; copy media into ``workspace/.media/``.

    Returns ``(cleaned_text, media_list)``:

    - ``cleaned_text`` has dragged image/audio paths replaced with the
      workspace-relative copy path. Document paths are left untouched.
    - ``media_list`` is the list of stable copy paths (workspace-relative
      strings) for population of ``InboundMessage.media``.

    Idempotent: re-dragging the same file content resolves to the same
    sha-keyed destination and is a no-op copy.
    """
    if not text:
        return text, []

    copied: list[str] = []
    media_dir = workspace / ".media"

    def _replace(match: re.Match) -> str:
        raw = match.group(1).replace("\\ ", " ").strip()
        # Strip trailing sentence punctuation that the regex greedily captured.
        raw = raw.rstrip(",.;:!?)")
        try:
            path = Path(raw).expanduser()
        except (OSError, ValueError):
            return match.group(0)

        try:
            if not path.is_absolute() or not path.is_file():
                return match.group(0)
        except OSError:
            return match.group(0)

        suffix = path.suffix.lower()
        if suffix not in _COPIED_EXTS:
            # Documents (markdown, txt, pdf, etc.) stay as-is so the agent's
            # read_file tool can resolve them directly.
            return match.group(0)

        try:
            content_hash = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError:
            return match.group(0)

        media_dir.mkdir(parents=True, exist_ok=True)
        dest = media_dir / f"{content_hash}{suffix}"
        if not dest.exists():
            try:
                shutil.copy2(path, dest)
            except OSError:
                return match.group(0)

        rel = dest.relative_to(workspace)
        rel_str = str(rel)
        if rel_str not in copied:
            copied.append(rel_str)
        return str(dest)

    cleaned = _PATH_RE.sub(_replace, text)
    return cleaned, copied
