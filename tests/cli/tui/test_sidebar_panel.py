"""Tests for the SidebarPanel widget."""

from __future__ import annotations

from unittest.mock import MagicMock

from durin.cli.tui.widgets.sidebar_panel import SidebarPanel


def _make_loop(
    *,
    todos: list[dict] | None = None,
    files: list[tuple[str, str]] | None = None,
    mcp_servers: dict | None = None,
    mcp_stacks: dict | None = None,
    workspace: str = "/tmp",
) -> MagicMock:
    """Build a mock AgentLoop with the attributes SidebarPanel reads."""
    loop = MagicMock()
    loop.workspace = workspace
    loop._mcp_servers = mcp_servers or {}
    loop._mcp_stacks = mcp_stacks or {}

    session = MagicMock()
    session.metadata = {"todos": todos} if todos is not None else {}
    loop.sessions.get_or_create.return_value = session
    return loop


class _HostApp:
    """Minimal host for mounting SidebarPanel."""

    def __init__(self) -> None:
        pass


# --- data gathering tests (no Textual app needed for pure logic) ---


def test_gather_todos_reads_session_metadata():
    panel = SidebarPanel()
    loop = _make_loop(todos=[{"content": "A", "status": "pending", "activeForm": "Doing A"}])
    panel.set_agent_loop(loop)
    panel.set_session_key("cli:test")
    todos = panel._gather_todos("cli:test")
    assert len(todos) == 1
    assert todos[0]["content"] == "A"


def test_gather_todos_empty_metadata():
    panel = SidebarPanel()
    loop = _make_loop(todos=None)
    panel.set_agent_loop(loop)
    assert panel._gather_todos("cli:test") == []


def test_gather_todos_no_loop():
    panel = SidebarPanel()
    assert panel._gather_todos("cli:test") == []


def test_gather_mcp_connected_servers():
    panel = SidebarPanel()
    loop = _make_loop(
        mcp_servers={"fetch": {}, "github": {}},
        mcp_stacks={"fetch": "stack"},
    )
    panel.set_agent_loop(loop)
    mcp = panel._gather_mcp()
    assert ("fetch", True) in mcp
    assert ("github", False) in mcp


def test_gather_mcp_no_servers():
    panel = SidebarPanel()
    loop = _make_loop(mcp_servers={})
    panel.set_agent_loop(loop)
    assert panel._gather_mcp() == []


def test_gather_files_no_loop():
    panel = SidebarPanel()
    assert panel._gather_files() == []


# --- render tests ---


def test_render_empty_state():
    panel = SidebarPanel()
    output = panel._format_content([], [], [])
    assert "TODO" in output
    assert "No todos" in output
    assert "FILES" in output
    assert "No changes" in output
    assert "MCP" in output
    assert "No MCP servers" in output


def test_render_with_todos():
    panel = SidebarPanel()
    todos = [
        {"content": "Run tests", "status": "completed", "activeForm": "Running tests"},
        {"content": "Fix bug", "status": "in_progress", "activeForm": "Fixing bug"},
        {"content": "Write docs", "status": "pending", "activeForm": "Writing docs"},
    ]
    output = panel._format_content(todos, [], [])
    assert "Run tests" in output
    assert "Fixing bug" in output
    assert "Write docs" in output
    assert "(2 active)" in output  # 2 non-completed


def test_render_with_files():
    panel = SidebarPanel()
    files = [("M", "src/app.py"), ("?", "new_file.py")]
    output = panel._format_content([], files, [])
    assert "src/app.py" in output
    assert "new_file.py" in output
    assert "(2 changed)" in output


def test_render_with_mcp():
    panel = SidebarPanel()
    mcp = [("fetch", True), ("github", False)]
    output = panel._format_content([], [], mcp)
    assert "fetch" in output
    assert "github" in output
    assert "(1/2)" in output  # 1 connected out of 2


def test_render_truncates_long_file_list():
    panel = SidebarPanel()
    files = [("M", f"file_{i}.py") for i in range(25)]
    output = panel._format_content([], files, [])
    assert "file_0.py" in output
    assert "+5 more" in output


# --- visibility tests (need Textual app) ---


def test_toggle_makes_panel_visible():
    """Verify show/hide toggles the --visible CSS class."""
    import pytest

    pytest.importorskip("textual")
    from textual.app import App, ComposeResult

    class _TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield SidebarPanel()

    async def run():
        app = _TestApp()
        async with app.run_test():
            panel = app.query_one(SidebarPanel)
            # Open by default (on_mount calls show_sidebar).
            assert panel.is_visible
            panel.hide_sidebar()
            assert not panel.is_visible
            panel.show_sidebar()
            assert panel.is_visible

    import asyncio

    asyncio.new_event_loop().run_until_complete(run())


def test_toggle_switches_visibility():
    """Verify toggle() flips visibility both ways."""
    import pytest

    pytest.importorskip("textual")
    from textual.app import App, ComposeResult

    class _TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield SidebarPanel()

    async def run():
        app = _TestApp()
        async with app.run_test():
            panel = app.query_one(SidebarPanel)
            initial = panel.is_visible
            panel.toggle()
            assert panel.is_visible != initial
            panel.toggle()
            assert panel.is_visible == initial

    import asyncio

    asyncio.new_event_loop().run_until_complete(run())


def test_update_work_tracks_active_and_renders():
    panel = SidebarPanel()
    panel.update_work({
        "name": "workflow_progress", "phase": "running",
        "call_id": "workflow:r1",
        "arguments": {"workflow": "review-changes"},
        "nodes": [{"id": "scan", "label": "scan", "status": "running", "route_label": None}],
    })
    assert panel.has_active_work is True
    content = panel._format_content([], [], [], {})
    assert "WORK" in content and "review-changes" in content


def test_work_clears_when_finished():
    panel = SidebarPanel()
    panel.update_work({"name": "subagent_result", "phase": "running",
                       "call_id": "subagent:t1", "label": "explore"})
    assert panel.has_active_work is True
    panel.update_work({"name": "subagent_result", "phase": "end",
                       "call_id": "subagent:t1", "label": "explore",
                       "result": "done"})
    assert panel.has_active_work is False


def test_version_is_in_discreet_footer_not_an_info_section():
    panel = SidebarPanel()
    info = {"model": "glm-5.2", "mode": "build", "version": "v0.1.0a11",
            "workdir": "/home/u/.durin/workspace"}
    content = panel._format_content([], [], [], info)
    # No INFO header; section order is WORK/TODO/FILES/MCP.
    assert "INFO" not in content
    assert content.index("TODO") < content.index("FILES") < content.index("MCP")
    # Version + model/mode live in the dim footer at the end.
    assert "sidebar-foot" in content
    assert "v0.1.0a11" in content
    assert content.rindex("v0.1.0a11") > content.index("MCP")
