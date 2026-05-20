"""prompt_toolkit completers for the interactive CLI.

Two completers wired into the same PromptSession via ``merge_completers``:

- :class:`FileReferenceCompleter` — triggers after ``@`` and offers
  workspace files (substring match, case-insensitive, depth-first
  walk with sensible excludes).
- :class:`ModelPresetCompleter` — triggers after ``/model `` and
  offers the configured model preset names.

Both are intentionally simple (no fzf-style scoring) for V1. Caching
keeps the walk cheap: the file list is computed once per session and
can be invalidated explicitly if the workspace tree changes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable, Iterator

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

__all__ = ["FileReferenceCompleter", "ModelPresetCompleter"]


_EXCLUDE_DIRS = frozenset({
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".durin",
})


class FileReferenceCompleter(Completer):
    """Completes ``@<prefix>`` with workspace-relative file paths."""

    MAX_FILES = 1000

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.expanduser().resolve()
        self._cached: list[str] | None = None

    def invalidate(self) -> None:
        """Drop the cached file list; next completion call re-scans."""
        self._cached = None

    def _scan_files(self) -> list[str]:
        if self._cached is not None:
            return self._cached
        out: list[str] = []
        for root, dirs, files in os.walk(self._workspace):
            # In-place mutation of dirs prunes the walk.
            dirs[:] = sorted(
                d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith(".")
            )
            for name in sorted(files):
                if name.startswith("."):
                    continue
                full = Path(root) / name
                try:
                    rel = full.relative_to(self._workspace)
                except ValueError:
                    continue
                out.append(str(rel))
                if len(out) >= self.MAX_FILES:
                    self._cached = out
                    return out
        self._cached = out
        return out

    def get_completions(
        self,
        document: Document,
        complete_event,
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        at_idx = text.rfind("@")
        if at_idx == -1:
            return
        # @ must follow whitespace or sit at start of input.
        if at_idx > 0 and not text[at_idx - 1].isspace():
            return
        prefix = text[at_idx + 1 :]
        if any(c.isspace() for c in prefix):
            return
        prefix_low = prefix.lower()
        for path in self._scan_files():
            if prefix_low and prefix_low not in path.lower():
                continue
            yield Completion(
                path,
                start_position=-len(prefix),
                display=path,
            )


class ModelPresetCompleter(Completer):
    """Completes ``/model <prefix>`` with configured preset names."""

    def __init__(self, presets_getter: Callable[[], Iterable[str]]) -> None:
        self._presets_getter = presets_getter

    def get_completions(
        self,
        document: Document,
        complete_event,
    ) -> Iterator[Completion]:
        text = document.text_before_cursor
        prefix_marker = "/model "
        if not text.startswith(prefix_marker):
            return
        prefix = text[len(prefix_marker) :]
        if any(c.isspace() for c in prefix):
            return
        prefix_low = prefix.lower()
        for preset in self._presets_getter():
            if prefix_low and not preset.lower().startswith(prefix_low):
                continue
            yield Completion(
                preset,
                start_position=-len(prefix),
                display=preset,
            )
