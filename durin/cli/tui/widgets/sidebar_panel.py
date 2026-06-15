"""SidebarPanel — collapsible left sidebar with Todos, Files, and MCP sections.

Toggled with Ctrl+B. Refreshes on open, every 5 seconds while visible,
and after each turn completes. Data is pulled lazily from the agent loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.widgets import Static

if TYPE_CHECKING:
    from durin.agent.loop import AgentLoop

__all__ = ["SidebarPanel"]

_REFRESH_INTERVAL = 5.0


class SidebarPanel(Static):
    """Collapsible sidebar showing Todos, modified Files, and MCP servers."""

    DEFAULT_CSS = """
    SidebarPanel {
        width: 34;
        min-width: 28;
        max-width: 48;
        height: 1fr;
        display: none;
        background: $surface;
        border-right: solid $accent;
        padding: 0 1;
        color: $text;
    }

    SidebarPanel.--visible {
        display: block;
    }

    SidebarPanel .sidebar-section-header {
        color: $accent;
        text-style: bold;
        padding: 1 0 0 0;
    }

    SidebarPanel .sidebar-count {
        color: $text-muted;
        text-style: italic;
    }

    SidebarPanel .sidebar-item {
        padding: 0 1;
        color: $text;
    }

    SidebarPanel .sidebar-empty {
        color: $text-muted;
        text-style: italic;
        padding: 0 1;
    }

    SidebarPanel .sidebar-done {
        color: $success;
    }

    SidebarPanel .sidebar-active {
        color: $warning;
        text-style: bold;
    }

    SidebarPanel .sidebar-pending {
        color: $text-muted;
    }

    SidebarPanel .sidebar-modified {
        color: $warning;
    }

    SidebarPanel .sidebar-untracked {
        color: $primary;
    }

    SidebarPanel .sidebar-connected {
        color: $success;
    }

    SidebarPanel .sidebar-disconnected {
        color: $error;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._agent_loop: AgentLoop | None = None
        self._session_key: str | None = None
        self._timer: Any = None

    def set_agent_loop(self, loop: AgentLoop | None) -> None:
        self._agent_loop = loop

    def set_session_key(self, key: str | None) -> None:
        self._session_key = key

    def on_unmount(self) -> None:
        self._stop_timer()

    # ---- visibility --------------------------------------------------------

    def show_sidebar(self) -> None:
        self.add_class("--visible")
        self.refresh_content()
        self._start_timer()

    def hide_sidebar(self) -> None:
        self.remove_class("--visible")
        self._stop_timer()

    def toggle(self) -> None:
        if self.has_class("--visible"):
            self.hide_sidebar()
        else:
            self.show_sidebar()

    @property
    def is_visible(self) -> bool:
        return self.has_class("--visible")

    # ---- refresh timer -----------------------------------------------------

    def _start_timer(self) -> None:
        self._stop_timer()
        self._timer = self.set_interval(_REFRESH_INTERVAL, self.refresh_content)

    def _stop_timer(self) -> None:
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.stop()
            self._timer = None

    def refresh_content(self) -> None:
        """Re-render the sidebar from live data sources."""
        if not self.is_visible:
            return
        todos = self._gather_todos(self._session_key)
        files = self._gather_files()
        mcp = self._gather_mcp()
        self.update(self._format_content(todos, files, mcp))

    # ---- data gathering ----------------------------------------------------

    def _gather_todos(self, session_key: str | None = None) -> list[dict[str, str]]:
        loop = self._agent_loop
        if loop is None:
            return []
        try:
            from durin.session.todo_state import parse_todos, todos_raw

            if session_key is None:
                return []
            session = loop.sessions.get_or_create(session_key)
            return parse_todos(todos_raw(session.metadata)) or []
        except Exception:  # noqa: BLE001
            return []

    def _gather_files(self) -> list[tuple[str, str]]:
        """Return list of (marker, path) tuples from ``git status --porcelain``."""
        loop = self._agent_loop
        if loop is None:
            return []
        workspace = getattr(loop, "workspace", None)
        if not workspace:
            return []
        try:
            import subprocess

            result = subprocess.run(  # noqa: S603, S602
                ["git", "status", "--porcelain"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=3,
            )
            files: list[tuple[str, str]] = []
            for line in result.stdout.strip().splitlines():
                if not line:
                    continue
                marker = line[:2].strip()
                path = line[3:]
                files.append((marker or "?", path))
            return files
        except Exception:  # noqa: BLE001
            return []

    def _gather_mcp(self) -> list[tuple[str, bool]]:
        """Return list of (server_name, connected) tuples."""
        loop = self._agent_loop
        if loop is None:
            return []
        servers = getattr(loop, "_mcp_servers", {}) or {}
        stacks = getattr(loop, "_mcp_stacks", {}) or {}
        if not servers:
            return []
        return [(name, name in stacks) for name in servers]

    # ---- rendering ---------------------------------------------------------

    def _format_content(
        self,
        todos: list[dict[str, str]],
        files: list[tuple[str, str]],
        mcp: list[tuple[str, bool]],
    ) -> str:
        lines: list[str] = []

        # --- Todos section ---
        pending = sum(1 for t in todos if t["status"] != "completed")
        lines.append(f"[sidebar-section-header]TODO[/] [sidebar-count]({pending} active)[/]")
        if not todos:
            lines.append("[sidebar-empty]No todos[/]")
        else:
            for t in todos:
                status = t["status"]
                if status == "completed":
                    cls = "sidebar-done"
                    mark = "\u2713"
                    text = t["content"]
                elif status == "in_progress":
                    cls = "sidebar-active"
                    mark = "\u25CB"
                    text = t.get("activeForm") or t["content"]
                else:
                    cls = "sidebar-pending"
                    mark = "\u25CB"
                    text = t["content"]
                lines.append(f"[{cls}]{mark} {text}[/]")
        lines.append("")

        # --- Files section ---
        lines.append(
            f"[sidebar-section-header]FILES[/] [sidebar-count]({len(files)} changed)[/]"
        )
        if not files:
            lines.append("[sidebar-empty]No changes[/]")
        else:
            for marker, path in files[:20]:
                if "?" in marker:
                    cls = "sidebar-untracked"
                    display = f"? {path}"
                elif "M" in marker:
                    cls = "sidebar-modified"
                    display = f"M {path}"
                else:
                    cls = "sidebar-item"
                    display = f"{marker} {path}"
                lines.append(f"[{cls}]{display}[/]")
            if len(files) > 20:
                lines.append(f"[sidebar-empty]\u2026 +{len(files) - 20} more[/]")
        lines.append("")

        # --- MCP section ---
        connected = sum(1 for _, ok in mcp if ok)
        lines.append(
            f"[sidebar-section-header]MCP[/] [sidebar-count]({connected}/{len(mcp)})[/]"
        )
        if not mcp:
            lines.append("[sidebar-empty]No MCP servers[/]")
        else:
            for name, ok in mcp:
                cls = "sidebar-connected" if ok else "sidebar-disconnected"
                dot = "\u25CF" if ok else "\u25CB"
                lines.append(f"[{cls}]{dot} {name}[/]")

        return "\n".join(lines)
