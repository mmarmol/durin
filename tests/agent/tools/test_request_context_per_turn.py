"""Every context-aware tool keeps its request context per turn.

One tool instance serves every turn running concurrently: the interactive
lane runs up to 4 sessions at once, and ``process_direct`` (cron,
automations, API) shares the same tool instances with the interactive
lane. A tool that stored its request context as a plain attribute
(``self._request_ctx = ctx``) would read whichever turn's ``set_context``
call landed last, not the turn now calling it — the exact race that put a
question, a file write, a secret request or a todo list in the wrong chat.

The parametrized test below builds one instance of each of the 14
converted tools and drives two overlapping asyncio tasks against it, each
setting its own context and then reading it back. After the fix (a
``RequestContextVar`` — a per-instance ``ContextVar``), each task's read
sees only the context it set, because asyncio gives every task its own
copy of the context. Before the fix, the shared attribute lets one task's
``set_context`` bleed into the other.

The two tests at the end reproduce the same race through real tool
behavior for the two highest-risk tools named in the bug report:
``ask_user_question`` (a question posted to the wrong chat) and
``write_file`` (a write landing in the wrong chat's work directory).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from durin.agent.tools.ask_user import PENDING_QUESTION_KEY, AskUserQuestionTool
from durin.agent.tools.context import RequestContext
from durin.agent.tools.filesystem import WriteFileTool
from durin.agent.tools.long_task import LongTaskTool
from durin.agent.tools.mcp_manage import McpManageTool
from durin.agent.tools.memory_ingest import MemoryIngestTool
from durin.agent.tools.note_decision import NoteDecisionTool
from durin.agent.tools.plan_mode import EnterPlanModeTool
from durin.agent.tools.secrets import RequestSecretTool
from durin.agent.tools.self import MyTool
from durin.agent.tools.session_search import SessionSearchTool
from durin.agent.tools.sleep import SleepTool
from durin.agent.tools.subagent_lifecycle import SubagentMonitorTool
from durin.agent.tools.tasks_tool import TasksTool
from durin.agent.tools.todos import TodoWriteTool
from durin.session.manager import SessionManager

_ALL_TOOL_NAMES = (
    "ask_user", "filesystem", "long_task", "mcp_manage", "memory_ingest",
    "note_decision", "plan_mode", "secrets", "self", "session_search",
    "sleep", "subagent_lifecycle", "tasks_tool", "todos",
)


def _build_tool(name: str, tmp_path: Path, sessions: SessionManager):
    if name == "ask_user":
        return AskUserQuestionTool(sessions=sessions, blocking=False)
    if name == "filesystem":
        return WriteFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    if name == "long_task":
        return LongTaskTool(sessions=sessions)
    if name == "mcp_manage":
        return McpManageTool(service=MagicMock())
    if name == "memory_ingest":
        return MemoryIngestTool(workspace=str(tmp_path))
    if name == "note_decision":
        return NoteDecisionTool(sessions=sessions, max_entries=10, max_chars=1500)
    if name == "plan_mode":
        return EnterPlanModeTool(sessions=sessions, workspace=tmp_path)
    if name == "secrets":
        return RequestSecretTool(sessions=sessions)
    if name == "self":
        return MyTool(runtime_state=MagicMock())
    if name == "session_search":
        return SessionSearchTool(sessions=sessions)
    if name == "sleep":
        return SleepTool()
    if name == "subagent_lifecycle":
        return SubagentMonitorTool(manager=MagicMock())
    if name == "tasks_tool":
        return TasksTool(workspace=str(tmp_path), subagent_manager=None, sessions=None)
    if name == "todos":
        return TodoWriteTool(sessions=sessions)
    raise ValueError(f"no factory for tool {name!r}")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", _ALL_TOOL_NAMES)
async def test_each_tool_reads_its_own_turns_context(tmp_path, tool_name):
    sessions = SessionManager(tmp_path)
    tool = _build_tool(tool_name, tmp_path, sessions)

    ctx_a = RequestContext(channel="cli", chat_id="a", session_key="session-A")
    ctx_b = RequestContext(channel="cli", chat_id="b", session_key="session-B")
    seen: dict[str, str | None] = {}

    async def turn_a():
        tool.set_context(ctx_a)
        # Yield twice so turn_b's set_context runs while turn_a is
        # suspended — the same interleaving a shared attribute loses.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        got = tool._ctx.get()
        seen["a"] = got.session_key if got else None

    async def turn_b():
        tool.set_context(ctx_b)
        got = tool._ctx.get()
        seen["b"] = got.session_key if got else None

    await asyncio.gather(turn_a(), turn_b())

    assert seen["a"] == "session-A"
    assert seen["b"] == "session-B"


@pytest.mark.asyncio
async def test_ask_user_question_posts_to_its_own_session_when_interleaved(tmp_path):
    """Two interleaved turns must not cross-post their questions."""
    sessions = SessionManager(tmp_path)
    tool = AskUserQuestionTool(sessions=sessions, blocking=False)

    ctx_a = RequestContext(channel="cli", chat_id="a", session_key="cli:a")
    ctx_b = RequestContext(channel="cli", chat_id="b", session_key="cli:b")

    async def turn_a():
        tool.set_context(ctx_a)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await tool.execute(question="Question from A?")

    async def turn_b():
        tool.set_context(ctx_b)
        await tool.execute(question="Question from B?")

    await asyncio.gather(turn_a(), turn_b())

    sess_a = sessions.get_or_create("cli:a")
    sess_b = sessions.get_or_create("cli:b")
    assert sess_a.metadata[PENDING_QUESTION_KEY]["question"] == "Question from A?"
    assert sess_b.metadata[PENDING_QUESTION_KEY]["question"] == "Question from B?"


@pytest.mark.asyncio
async def test_write_file_resolves_its_own_work_dir_when_interleaved(tmp_path):
    """Two interleaved turns must each land in their own chat's work dir."""
    tool = WriteFileTool(workspace=tmp_path, allowed_dir=tmp_path)

    ctx_a = RequestContext(channel="telegram", chat_id="a", session_key="telegram:a")
    ctx_b = RequestContext(channel="telegram", chat_id="b", session_key="telegram:b")

    async def turn_a():
        tool.set_context(ctx_a)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await tool.execute(path="note.md", content="from A")

    async def turn_b():
        tool.set_context(ctx_b)
        await tool.execute(path="note.md", content="from B")

    await asyncio.gather(turn_a(), turn_b())

    assert (tmp_path / "work" / "telegram_a" / "note.md").read_text() == "from A"
    assert (tmp_path / "work" / "telegram_b" / "note.md").read_text() == "from B"
