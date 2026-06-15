"""Full-page diff viewer screen (Ctrl+G).

Shows working-tree changes (``git status`` + per-file ``git diff``) in a
two-pane layout: file list on the left, colored diff content on the right.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.text import Text
from textual.app import Screen
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Label, OptionList, RichLog
from textual.widgets.option_list import Option


def _run_git(args: list[str], cwd: Path) -> str:
    """Run a git command, return stdout as text (empty on error)."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout
    except Exception:  # noqa: BLE001
        return ""


def _parse_porcelain(text: str) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain`` output → [(marker, path), ...]."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        marker_raw = line[:2].strip()
        marker = marker_raw[0] if marker_raw else "?"
        path = line[3:].strip()
        if not path:
            continue
        # Rename: "old -> new" → take new
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        out.append((marker, path))
    return out


def _render_diff(diff_text: str) -> Text:
    """Render unified diff text as colored Rich Text."""
    if not diff_text.strip():
        return Text("(no changes)", style="dim")
    out = Text()
    for line in diff_text.splitlines():
        if line.startswith("diff --git") or line.startswith("index "):
            out.append(line + "\n", style="bold")
        elif line.startswith("+++") or line.startswith("---"):
            out.append(line + "\n", style="bold cyan")
        elif line.startswith("+"):
            out.append(line + "\n", style="green")
        elif line.startswith("-"):
            out.append(line + "\n", style="red")
        elif line.startswith("@@"):
            out.append(line + "\n", style="cyan")
        else:
            out.append(line + "\n", style="dim")
    return out


class DiffViewerScreen(Screen[None]):
    """Full-page diff viewer with file list + diff content."""

    CSS = """
    DiffViewerScreen {
        layout: horizontal;
    }
    #diff-file-list {
        width: 32;
        height: 100%;
        border-right: solid $accent;
        background: $surface;
        overflow-y: auto;
    }
    #diff-file-list Label {
        padding: 0 1;
        background: $boost;
        color: $text;
        text-style: bold;
    }
    #diff-file-list OptionList {
        height: 1fr;
    }
    #diff-content-area {
        width: 1fr;
        height: 100%;
    }
    #diff-header {
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $text;
        text-style: bold;
    }
    #diff-content {
        height: 1fr;
        border: none;
    }
    #diff-empty {
        height: 1fr;
        content-align: center middle;
        color: $text-disabled;
    }
    """

    BINDINGS = [
        Binding("q", "close_viewer", "Close"),
        Binding("escape", "close_viewer", "Close"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, workspace: Path) -> None:
        super().__init__()
        self._workspace = workspace
        self._files: list[tuple[str, str]] = []

    @property
    def workspace(self) -> Path:
        """Workspace path for git commands."""
        return self._workspace

    @property
    def files(self) -> list[tuple[str, str]]:
        """List of (marker, path) tuples from the last status read."""
        return self._files

    def compose(self) -> object:
        yield Horizontal(
            Vertical(
                Label("Changed files"),
                OptionList(id="diff-file-list"),
                id="diff-file-panel",
            ),
            Vertical(
                Label("Select a file →", id="diff-header"),
                RichLog(id="diff-content", markup=True),
                id="diff-content-area",
            ),
        )

    def on_mount(self) -> None:
        self._refresh_files()

    def _refresh_files(self) -> None:
        """Re-read git status and populate the file list."""
        self._files = _parse_porcelain(
            _run_git(["status", "--porcelain"], self._workspace)
        )
        ol = self.query_one("#diff-file-list", OptionList)
        ol.clear_options()
        if not self._files:
            header = self.query_one("#diff-header", Label)
            header.update("No changes detected — working tree is clean")
            return
        for marker, path in self._files:
            label = f"{marker:>2}  {path}"
            ol.add_option(Option(label, id=path))
        # auto-select first file
        if ol.option_count > 0:
            first = ol.get_option_at_index(0)
            self._show_file(first.id)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self._show_file(event.option.id)

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option and event.option.id:
            self._show_file(event.option.id)

    def _show_file(self, path: str) -> None:
        """Fetch and render the diff for a single file."""
        header = self.query_one("#diff-header", Label)
        diff_log = self.query_one("#diff-content", RichLog)
        diff_text = _run_git(["diff", "HEAD", "--", path], self._workspace)
        # If no diff against HEAD (new untracked file), show file content as all-additions
        if not diff_text.strip() and path in [p for _, p in self._files]:
            diff_text = _run_git(["diff", "--no-index", "/dev/null", path], self._workspace)
        header.update(path)
        diff_log.clear()
        diff_log.write(_render_diff(diff_text))

    def action_close_viewer(self) -> None:
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self._refresh_files()
