"""Tests for ToolCallBubble — the visual block for one agent tool invocation.

Covers:
- Lifecycle (start → end → error) transitions update CSS class and body.
- edit_file renders a coloured unified diff.
- exec renders IN / OUT with a visual separator.
- The `[copy]` button calls the shared clipboard helper with the right
  payload per tool (exec → output only; edit_file → before/after diff).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from durin.cli.tui.app import DurinApp
from durin.cli.tui.widgets import ToolCallBubble
from durin.cli.tui.widgets.chat_view import ChatView


@pytest.mark.asyncio
async def test_tool_call_bubble_starts_in_running_state() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        bubble = ToolCallBubble({
            "version": 1, "phase": "start", "call_id": "x1",
            "name": "edit_file",
            "arguments": {"path": "a.py", "old_text": "x", "new_text": "y"},
        })
        chat.mount(bubble)
        await pilot.pause()
        assert bubble.has_class("running")
        assert not bubble.has_class("ok")
        assert not bubble.has_class("error")


@pytest.mark.asyncio
async def test_tool_call_bubble_phase_end_marks_ok() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        bubble = ToolCallBubble({
            "version": 1, "phase": "start", "call_id": "x2",
            "name": "exec", "arguments": {"command": "echo hi"},
        })
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event({
            "version": 1, "phase": "end", "call_id": "x2",
            "name": "exec", "arguments": {"command": "echo hi"},
            "result": "hi",
        })
        await pilot.pause()
        assert bubble.has_class("ok")
        assert not bubble.has_class("running")
        assert not bubble.has_class("error")


@pytest.mark.asyncio
async def test_tool_call_bubble_phase_error_marks_error() -> None:
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        bubble = ToolCallBubble({
            "version": 1, "phase": "start", "call_id": "x3",
            "name": "exec", "arguments": {"command": "bad-cmd"},
        })
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event({
            "version": 1, "phase": "error", "call_id": "x3",
            "name": "exec", "arguments": {"command": "bad-cmd"},
            "error": "command not found",
        })
        await pilot.pause()
        assert bubble.has_class("error")


@pytest.mark.asyncio
async def test_edit_file_body_contains_diff_lines() -> None:
    """edit_file body must show a unified diff with +/- markers."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        bubble = ToolCallBubble({
            "version": 1, "phase": "end", "call_id": "x4",
            "name": "edit_file",
            "arguments": {
                "path": "README.md",
                "old_text": "pipx install durin",
                "new_text": "pipx install --pre durin-agent",
            },
            "result": {"output": "Modified"},
        })
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event({
            "version": 1, "phase": "end", "call_id": "x4",
            "name": "edit_file",
            "arguments": {
                "path": "README.md",
                "old_text": "pipx install durin",
                "new_text": "pipx install --pre durin-agent",
            },
            "result": {"output": "Modified"},
        })
        # Expand the bubble so the diff is visible (2-line preview
        # would otherwise hide it).
        bubble._expanded = True
        bubble._rerender_body_with_truncation()
        await pilot.pause()
        body = _body_plain(bubble)
        assert "-pipx install durin" in body
        assert "+pipx install --pre durin-agent" in body


@pytest.mark.asyncio
async def test_exec_body_shows_command_then_output_when_expanded() -> None:
    """exec body shows `$ command` line, then output (no IN/OUT labels)."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        bubble = ToolCallBubble({
            "version": 1, "phase": "start", "call_id": "x5",
            "name": "exec", "arguments": {"command": "ls -la"},
        })
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event({
            "version": 1, "phase": "end", "call_id": "x5",
            "name": "exec", "arguments": {"command": "ls -la"},
            "result": "total 0\ndrwxr-xr-x  3 user  staff   96 Jan  1 12:00 .",
        })
        await pilot.pause()
        # Expand so the full output is visible.
        bubble._expanded = True
        bubble._rerender_body_with_truncation()
        await pilot.pause()
        body = _body_plain(bubble)
        # No more IN/OUT scaffolding — command line is `$ <cmd>`, then output.
        assert "IN" not in body or body.index("$ ") < (body.index("IN") if "IN" in body else 9999)
        assert "OUT" not in body or body.index("$ ") < (body.index("OUT") if "OUT" in body else 9999)
        assert "$ ls -la" in body
        assert "drwxr-xr-x" in body


@pytest.mark.asyncio
async def test_copy_button_copies_exec_output_only() -> None:
    """For exec, the clipboard should receive the OUTPUT, not the command."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        bubble = ToolCallBubble({
            "version": 1, "phase": "start", "call_id": "x6",
            "name": "exec", "arguments": {"command": "echo hello"},
        })
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event({
            "version": 1, "phase": "end", "call_id": "x6",
            "name": "exec", "arguments": {"command": "echo hello"},
            "result": "hello",
        })
        await pilot.pause()
        with patch("durin.utils.clipboard.copy_text") as mock_copy:
            mock_copy.return_value = "pbcopy"
            bubble._copy_body_to_clipboard()
        mock_copy.assert_called_once()
        copied = mock_copy.call_args.args[0]
        assert "hello" in copied
        assert "echo hello" not in copied, "command should NOT be in clipboard text"


@pytest.mark.asyncio
async def test_copy_button_copies_edit_file_diff() -> None:
    """For edit_file, the clipboard should receive a before/after snapshot."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        bubble = ToolCallBubble({
            "version": 1, "phase": "end", "call_id": "x7",
            "name": "edit_file",
            "arguments": {"path": "a.py", "old_text": "foo", "new_text": "bar"},
            "result": {"output": "ok"},
        })
        chat.mount(bubble)
        await pilot.pause()
        with patch("durin.utils.clipboard.copy_text") as mock_copy:
            mock_copy.return_value = "pbcopy"
            bubble._copy_body_to_clipboard()
        copied = mock_copy.call_args.args[0]
        assert "foo" in copied
        assert "bar" in copied


@pytest.mark.asyncio
async def test_copy_button_click_fires_copy() -> None:
    """A pilot.click on the `[copy]` Static must trigger a clipboard write."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        bubble = ToolCallBubble({
            "version": 1, "phase": "end", "call_id": "x8",
            "name": "exec", "arguments": {"command": "uname"},
            "result": "Darwin",
        })
        chat.mount(bubble)
        await pilot.pause()
        # Cause an end-event so the body holds the real output.
        bubble.update_from_event({
            "version": 1, "phase": "end", "call_id": "x8",
            "name": "exec", "arguments": {"command": "uname"},
            "result": "Darwin",
        })
        await pilot.pause()
        with patch("durin.utils.clipboard.copy_text") as mock_copy:
            mock_copy.return_value = "pbcopy"
            await pilot.click("#tc-copy")
        # Either the click went through and we got a call, or Textual's
        # event routing didn't propagate (in which case the unit-level
        # `_copy_body_to_clipboard` test above still proves the logic).
        # We tolerate the latter only if the unit test passes — assert
        # for both here so the click path is exercised at least once.
        assert mock_copy.called, "click on [copy] did not trigger copy_text"


@pytest.mark.asyncio
async def test_list_dir_strips_decorative_icons_from_body() -> None:
    """list_dir output with 📁 📄 icons must render WITHOUT them — no '80s look."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        decorated = "📁 .durin\n📄 .gitignore\n📁 memory\n📄 SOUL.md"
        bubble = ToolCallBubble({
            "version": 1, "phase": "end", "call_id": "ld1",
            "name": "list_dir", "arguments": {"path": "."},
            "result": decorated,
        })
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event({
            "version": 1, "phase": "end", "call_id": "ld1",
            "name": "list_dir", "arguments": {"path": "."},
            "result": decorated,
        })
        # Expand the bubble so all 4 lines are visible (preview = 2).
        bubble._expanded = True
        bubble._rerender_body_with_truncation()
        await pilot.pause()
        body = _body_plain(bubble)
        # Decorations gone from display.
        for icon in ("📁", "📂", "📄", "📝"):
            assert icon not in body, f"{icon!r} should not appear in rendered body"
        # Names still present.
        for name in (".durin", ".gitignore", "memory", "SOUL.md"):
            assert name in body


@pytest.mark.asyncio
async def test_copy_strips_decorations_from_list_dir() -> None:
    """Clipboard text from a list_dir copy must contain raw paths, no icons."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        decorated = "📁 .durin\n📄 SOUL.md\n📁 memory"
        bubble = ToolCallBubble({
            "version": 1, "phase": "end", "call_id": "ld2",
            "name": "list_dir", "arguments": {"path": "."},
            "result": decorated,
        })
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event({
            "version": 1, "phase": "end", "call_id": "ld2",
            "name": "list_dir", "arguments": {"path": "."},
            "result": decorated,
        })
        await pilot.pause()
        with patch("durin.utils.clipboard.copy_text") as mock_copy:
            mock_copy.return_value = "pbcopy"
            bubble._copy_body_to_clipboard()
        copied = mock_copy.call_args.args[0]
        for icon in ("📁", "📂", "📄"):
            assert icon not in copied
        assert ".durin" in copied
        assert "SOUL.md" in copied


def _body_plain(bubble: ToolCallBubble) -> str:
    """Extract the plain text Textual would render in the body Static."""
    from textual.widgets import Static

    body = bubble.query_one("#tc-body", Static)
    content = body._Static__content  # type: ignore[attr-defined]
    if hasattr(content, "plain"):
        return content.plain  # type: ignore[no-any-return]
    return str(content)


def _expand_toggle_text(bubble) -> str:
    """Read whatever label the [expand] / [collapse] toggle currently shows."""
    from textual.widgets import Static as _Static

    toggle = bubble.query_one("#tc-expand", _Static)
    content = toggle._Static__content  # type: ignore[attr-defined]
    if hasattr(content, "plain"):
        return content.plain  # type: ignore[no-any-return]
    return str(content)


# ---------------------------------------------------------------------------
# Truncation + expand toggle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_output_is_truncated_to_preview_lines() -> None:
    """Default render keeps only PREVIEW_LINES; the rest is hidden."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        # 10 lines of output → 2 visible (PREVIEW_LINES default).
        long = "\n".join(f"line {i}" for i in range(10))
        bubble = ToolCallBubble({
            "version": 1, "phase": "end", "call_id": "trunc1",
            "name": "list_dir", "arguments": {"path": "."},
            "result": long,
        })
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event({
            "version": 1, "phase": "end", "call_id": "trunc1",
            "name": "list_dir", "arguments": {"path": "."},
            "result": long,
        })
        await pilot.pause()
        body = _body_plain(bubble)
        assert "line 0" in body
        assert "line 1" in body
        # Lines 2-9 must be hidden in the default truncated view.
        assert "line 5" not in body
        # The toggle should say "+N more".
        assert "+" in _expand_toggle_text(bubble)
        assert "8 more" in _expand_toggle_text(bubble)


@pytest.mark.asyncio
async def test_expand_toggle_reveals_full_output() -> None:
    """Click on `[+N more]` flips state and shows everything."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        long = "\n".join(f"line {i}" for i in range(6))
        bubble = ToolCallBubble({
            "version": 1, "phase": "end", "call_id": "trunc2",
            "name": "list_dir", "arguments": {"path": "."},
            "result": long,
        })
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event({
            "version": 1, "phase": "end", "call_id": "trunc2",
            "name": "list_dir", "arguments": {"path": "."},
            "result": long,
        })
        await pilot.pause()
        # Initially truncated.
        assert "line 5" not in _body_plain(bubble)
        # Simulate clicking the toggle.
        bubble._toggle_expanded()
        await pilot.pause()
        body = _body_plain(bubble)
        # All 6 lines visible.
        for i in range(6):
            assert f"line {i}" in body
        # Toggle now reads `[collapse]`.
        assert "collapse" in _expand_toggle_text(bubble)


@pytest.mark.asyncio
async def test_collapse_after_expand_returns_to_preview() -> None:
    """Toggling twice returns to the truncated view."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        long = "\n".join(f"line {i}" for i in range(8))
        bubble = ToolCallBubble({
            "version": 1, "phase": "end", "call_id": "trunc3",
            "name": "list_dir", "arguments": {"path": "."},
            "result": long,
        })
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event({
            "version": 1, "phase": "end", "call_id": "trunc3",
            "name": "list_dir", "arguments": {"path": "."},
            "result": long,
        })
        await pilot.pause()
        bubble._toggle_expanded()  # expand
        await pilot.pause()
        bubble._toggle_expanded()  # collapse again
        await pilot.pause()
        assert "line 5" not in _body_plain(bubble)
        assert "more" in _expand_toggle_text(bubble)


@pytest.mark.asyncio
async def test_short_output_has_no_expand_toggle() -> None:
    """If the body fits in PREVIEW_LINES, the [expand] toggle is invisible."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        bubble = ToolCallBubble({
            "version": 1, "phase": "end", "call_id": "trunc4",
            "name": "list_dir", "arguments": {"path": "."},
            "result": "one line only",
        })
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event({
            "version": 1, "phase": "end", "call_id": "trunc4",
            "name": "list_dir", "arguments": {"path": "."},
            "result": "one line only",
        })
        await pilot.pause()
        # Toggle is empty when nothing was truncated.
        assert _expand_toggle_text(bubble).strip() == ""


# ---------------------------------------------------------------------------
# Interactive tools — ask_user_question, request_secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_user_question_body_shows_question_and_options() -> None:
    """ask_user_question renders the question + numbered options, never
    the internal YIELD instruction the raw tool result carries."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        event = {
            "version": 1, "phase": "end", "call_id": "aq1",
            "name": "ask_user_question",
            "arguments": {
                "question": "Which database should we use?",
                "options": ["Postgres", "SQLite", "DuckDB"],
            },
            "result": (
                "Question registered (id=abc): 'Which database should we use?'.\n"
                "YIELD TO USER. Present this exact question..."
            ),
        }
        bubble = ToolCallBubble(event)
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event(event)
        bubble._expanded = True
        bubble._rerender_body_with_truncation()
        await pilot.pause()
        body = _body_plain(bubble)
        assert "Which database should we use?" in body
        assert "Postgres" in body and "DuckDB" in body
        assert "YIELD TO USER" not in body


@pytest.mark.asyncio
async def test_request_secret_body_shows_set_command() -> None:
    """request_secret renders the set command, not the YIELD blob."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        event = {
            "version": 1, "phase": "end", "call_id": "rs1",
            "name": "request_secret",
            "arguments": {
                "name": "ATLASSIAN_API_TOKEN",
                "service": "atlassian",
                "purpose": "create Jira issues",
            },
            "result": (
                "Secret 'ATLASSIAN_API_TOKEN' is not stored.\n"
                "YIELD TO USER. Present this exact instruction..."
            ),
        }
        bubble = ToolCallBubble(event)
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event(event)
        bubble._expanded = True
        bubble._rerender_body_with_truncation()
        await pilot.pause()
        body = _body_plain(bubble)
        assert "ATLASSIAN_API_TOKEN" in body
        assert "create Jira issues" in body
        assert (
            "durin secret set ATLASSIAN_API_TOKEN --service atlassian --scope exec"
            in body
        )
        assert "YIELD TO USER" not in body


@pytest.mark.asyncio
async def test_request_secret_body_reports_already_stored() -> None:
    """When the credential already exists, say so instead of the command."""
    app = DurinApp(agent_loop=None)
    async with app.run_test() as pilot:
        chat = app.query_one(ChatView)
        event = {
            "version": 1, "phase": "end", "call_id": "rs2",
            "name": "request_secret",
            "arguments": {"name": "GITHUB_TOKEN", "service": "github"},
            "result": "Secret 'GITHUB_TOKEN' already exists (service=github, scope=exec).",
        }
        bubble = ToolCallBubble(event)
        chat.mount(bubble)
        await pilot.pause()
        bubble.update_from_event(event)
        bubble._expanded = True
        bubble._rerender_body_with_truncation()
        await pilot.pause()
        body = _body_plain(bubble)
        assert "already stored" in body
        assert "durin secret set" not in body
