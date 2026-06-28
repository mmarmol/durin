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
# Faster tick while work is in flight: advances the braille spinner so the panel
# visibly "breathes" instead of looking frozen. Re-renders from cached data —
# the expensive git/MCP gather still runs only on the slow interval.
_ANIM_INTERVAL = 0.25
_ANIM_TICKS_PER_GATHER = int(_REFRESH_INTERVAL / _ANIM_INTERVAL)


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
        border-left: solid $accent;
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

    SidebarPanel .work-header { color: $accent; text-style: bold; }
    SidebarPanel .work-count { color: $text-muted; text-style: italic; }
    SidebarPanel .work-finished-header { color: $text-muted; padding: 1 0 0 0; }
    SidebarPanel .work-running { color: $accent; }
    SidebarPanel .work-done { color: $success; }
    SidebarPanel .work-failed { color: $error; }
    SidebarPanel .work-pending { color: $text-muted; }

    SidebarPanel .sidebar-foot {
        color: $text-muted;
        text-style: italic;
        padding: 1 0 0 0;
    }
    """

    def __init__(self) -> None:
        super().__init__("")
        self._agent_loop: AgentLoop | None = None
        self._session_key: str | None = None
        self._timer: Any = None
        self._spin = 0
        self._ticks = 0
        # Last gathered (todos, files, mcp, info) — re-rendered on spinner ticks
        # without re-running the expensive git/MCP gather.
        self._cache: tuple[Any, Any, Any, Any] | None = None
        from durin.cli.tui.widgets.work_state import WorkStore

        self._work = WorkStore()

    def set_agent_loop(self, loop: AgentLoop | None) -> None:
        self._agent_loop = loop

    def set_session_key(self, key: str | None) -> None:
        self._session_key = key

    def on_mount(self) -> None:
        # The sidebar is open by default — it carries persistent context (work,
        # todos, changed files, servers) the user wants visible, not just the
        # current task. Ctrl+B still toggles it.
        self.show_sidebar()

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

    def update_work(self, event: dict) -> None:
        """Ingest one workflow/subagent progress event and re-render if visible."""
        self._work.ingest(event)
        if self.is_visible:
            self.refresh_content()

    @property
    def has_active_work(self) -> bool:
        return self._work.active_count() > 0

    def jump_to_work(self) -> None:
        """Make the sidebar visible so the WORK section is on screen."""
        if not self.is_visible:
            self.show_sidebar()
        else:
            self.refresh_content()

    @property
    def is_visible(self) -> bool:
        return self.has_class("--visible")

    # ---- refresh timer -----------------------------------------------------

    def _start_timer(self) -> None:
        self._stop_timer()
        # One fast interval: it advances the spinner and re-renders from cache
        # every tick, and does a full (git/MCP) gather once every N ticks.
        self._timer = self.set_interval(_ANIM_INTERVAL, self._tick)

    def _stop_timer(self) -> None:
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.stop()
            self._timer = None

    def _tick(self) -> None:
        """Spinner tick: cheap re-render from cache, full gather every N ticks."""
        if not self.is_visible:
            return
        self._ticks += 1
        if self.has_active_work:
            self._spin += 1
        if self._ticks % _ANIM_TICKS_PER_GATHER == 0 or self._cache is None:
            self.refresh_content()
        elif self.has_active_work:
            self._render_cached()

    def refresh_content(self) -> None:
        """Re-gather live data (git/MCP/todos) and re-render."""
        if not self.is_visible:
            return
        self._cache = (
            self._gather_todos(self._session_key),
            self._gather_files(),
            self._gather_mcp(),
            self._gather_info(),
        )
        self._render_cached()

    def _render_cached(self) -> None:
        """Re-render from the last gathered data (cheap — no git/MCP gather)."""
        if not self.is_visible or self._cache is None:
            return
        todos, files, mcp, info = self._cache
        self.update(self._format_content(todos, files, mcp, info))

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

    def _gather_info(self) -> dict[str, str]:
        """Return session/runtime info for the sidebar Info section."""
        info: dict[str, str] = {}
        loop = self._agent_loop
        if loop is not None:
            info["model"] = getattr(loop, "model", "?")
            ctx_window = getattr(loop, "context_window_tokens", 0)
            if ctx_window:
                info["ctx"] = f"{ctx_window // 1000}K"
            workspace = getattr(loop, "workspace", None)
            if workspace:
                info["workdir"] = str(workspace)
            try:
                from durin.agent.agent_mode import get_active_mode_name

                session = loop.sessions.get_or_create(self._session_key) if self._session_key else None
                if session is not None:
                    info["mode"] = get_active_mode_name(session)
            except Exception:  # noqa: BLE001
                pass
            # Context usage estimate
            if ctx_window and self._session_key:
                try:
                    used = self._estimate_context_tokens(loop, self._session_key)
                    if used > 0:
                        pct = min(100, used * 100 // ctx_window)
                        info["ctx"] = f"{used // 1000}K/{ctx_window // 1000}K ({pct}%)"
                except Exception:  # noqa: BLE001
                    pass
        try:
            from durin import __version__

            info["version"] = f"v{__version__}"
        except Exception:  # noqa: BLE001
            pass
        return info

    def _estimate_context_tokens(self, loop: Any, session_key: str) -> int:
        """Rough token estimate for the current session context."""
        try:
            from durin.utils.helpers import estimate_message_tokens

            session = loop.sessions.get_or_create(session_key)
            total = 0
            for msg in session.messages:
                total += estimate_message_tokens(msg)
            return total
        except Exception:  # noqa: BLE001
            return 0

    # ---- rendering ---------------------------------------------------------

    def _format_content(
        self,
        todos: list[dict[str, str]],
        files: list[tuple[str, str]],
        mcp: list[tuple[str, bool]],
        info: dict[str, str] | None = None,
    ) -> str:
        lines: list[str] = []

        # Section order is by dynamism: live work first, then the active task
        # list, the changed files, server status, and finally a discreet footer
        # of static reference info (model/mode/version/workspace — also in the
        # bottom bar, kept here dim for a glance without leaving the panel).

        # --- Work section (pushed; workflows + sub-agents; animated spinner) ---
        work_markup = self._work.render_markup(self._spin)
        if work_markup:
            lines.append(work_markup)
            lines.append("")

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

        # --- Discreet footer: static reference info ---
        if info:
            model_mode = " \u00B7 ".join(
                p for p in (info.get("model"), info.get("mode")) if p
            )
            wd = info.get("workdir", "")
            if len(wd) > 26:
                wd = "\u2026" + wd[-25:]
            version_line = " \u00B7 ".join(p for p in (info.get("version"), wd) if p)
            foot = [p for p in (model_mode, version_line) if p]
            if foot:
                lines.append("")
                for line in foot:
                    lines.append(f"[sidebar-foot]{line}[/]")

        return "\n".join(lines)
