"""Telemetry for the four tools we instrumented in the audit follow-up:
``list_dir``, ``web_search``, ``web_fetch``, ``todo_write``.

Before this work, only the Phase 1c set (``read_file``, ``edit_file``,
``grep``, ``repo_overview``, ``exec.spill``, plus the ask_* / sleep
families) was instrumented. The meta sidecar caught these tools'
invocations universally, but per-event schema (so dashboards can spot
"slow list_dir on 50k entries" or "web_search provider degradation")
was missing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_for_tool_module(monkeypatch, mod_path: str, sink: _RecordingTelemetry) -> None:
    """The helper at ``durin/agent/tools/_telemetry.py::emit_tool_event``
    resolves ``current_telemetry`` from its own module. Patch THAT
    binding so the recording sink receives events regardless of which
    tool emits them."""
    from durin.agent.tools import _telemetry as t
    monkeypatch.setattr(t, "current_telemetry", lambda: sink)


def _bind_for_filesystem_module(monkeypatch, sink: _RecordingTelemetry) -> None:
    """``list_dir`` lives in ``filesystem.py`` and uses the
    ``_FsTool._emit`` instance method (not the free helper). That
    method resolves ``current_telemetry`` from filesystem.py's own
    imports. Patch THAT binding."""
    from durin.agent.tools import filesystem as fs
    monkeypatch.setattr(fs, "current_telemetry", lambda: sink)


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dir_emits_telemetry(tmp_path, monkeypatch):
    """list_dir on a small directory → emits ``tool.list_dir`` with
    displayed/total/truncated fields populated."""
    from durin.agent.tools.filesystem import ListDirTool

    sink = _RecordingTelemetry()
    _bind_for_filesystem_module(monkeypatch, sink)

    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "sub").mkdir()

    tool = ListDirTool(workspace=tmp_path)
    result = await tool.execute(path=str(tmp_path))

    assert "a.txt" in result and "b.txt" in result
    events = [e for e in sink.events if e[0] == "tool.list_dir"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload["displayed"] == 3
    assert payload["total_before_cap"] == 3
    assert payload["truncated"] is False
    assert payload["recursive"] is False


@pytest.mark.asyncio
async def test_list_dir_telemetry_truncation_flag(tmp_path, monkeypatch):
    """When max_entries < total, ``truncated`` flips to True and
    ``total_before_cap`` reflects the real count."""
    from durin.agent.tools.filesystem import ListDirTool

    sink = _RecordingTelemetry()
    _bind_for_filesystem_module(monkeypatch, sink)

    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x")

    tool = ListDirTool(workspace=tmp_path)
    await tool.execute(path=str(tmp_path), max_entries=3)

    payload = [e for e in sink.events if e[0] == "tool.list_dir"][0][1]
    assert payload["displayed"] == 3
    assert payload["total_before_cap"] == 10
    assert payload["truncated"] is True


@pytest.mark.asyncio
async def test_list_dir_telemetry_empty_dir(tmp_path, monkeypatch):
    """Empty dir → still emit, with zero counts. Lets the operator see
    the call happened even when there's no data."""
    from durin.agent.tools.filesystem import ListDirTool

    sink = _RecordingTelemetry()
    _bind_for_filesystem_module(monkeypatch, sink)

    tool = ListDirTool(workspace=tmp_path)
    result = await tool.execute(path=str(tmp_path))

    assert "empty" in result.lower()
    events = [e for e in sink.events if e[0] == "tool.list_dir"]
    assert len(events) == 1
    assert events[0][1]["total_before_cap"] == 0


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_search_emits_telemetry_with_provider(monkeypatch):
    """``web_search`` records which provider served the call, the query
    length, and the requested count."""
    from durin.agent.tools.web import WebSearchTool

    sink = _RecordingTelemetry()
    _bind_for_tool_module(monkeypatch, "durin.agent.tools.web", sink)

    tool = WebSearchTool(config=MagicMock(provider="brave", max_results=5))
    # Stub the provider helper so no network call happens.
    monkeypatch.setattr(tool, "_search_brave", AsyncMock(return_value="result text"))

    await tool.execute(query="python tips", count=3)

    events = [e for e in sink.events if e[0] == "tool.web_search"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload["provider"] == "brave"
    assert payload["query_chars"] == len("python tips")
    assert payload["requested_count"] == 3
    assert payload["result_chars"] == len("result text")
    assert payload["error"] is False


@pytest.mark.asyncio
async def test_web_search_telemetry_marks_errors(monkeypatch):
    """When the provider helper returns ``Error: …``, the event's
    ``error`` field is True."""
    from durin.agent.tools.web import WebSearchTool

    sink = _RecordingTelemetry()
    _bind_for_tool_module(monkeypatch, "durin.agent.tools.web", sink)

    tool = WebSearchTool(config=MagicMock(provider="brave", max_results=5))
    monkeypatch.setattr(tool, "_search_brave", AsyncMock(return_value="Error: rate limited"))

    await tool.execute(query="x")
    payload = [e for e in sink.events if e[0] == "tool.web_search"][0][1]
    assert payload["error"] is True


@pytest.mark.asyncio
async def test_web_search_telemetry_unknown_provider(monkeypatch):
    """Unknown provider path also emits — operators want to see
    config-typo'd providers fail visibly, not silently."""
    from durin.agent.tools.web import WebSearchTool

    sink = _RecordingTelemetry()
    _bind_for_tool_module(monkeypatch, "durin.agent.tools.web", sink)

    tool = WebSearchTool(config=MagicMock(provider="nonexistent", max_results=5))
    result = await tool.execute(query="x")

    assert "unknown search provider" in result
    payload = [e for e in sink.events if e[0] == "tool.web_search"][0][1]
    assert payload["provider"] == "nonexistent"
    assert payload["error"] is True


# ---------------------------------------------------------------------------
# todo_write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_todo_write_emits_status_counts(monkeypatch, tmp_path):
    """The whole point of todo_write telemetry: see whether the model
    is advancing through todos (pending → in_progress → completed) or
    accumulating pending forever."""
    from durin.agent.tools.todos import TodoWriteTool
    from durin.session.manager import SessionManager

    sink = _RecordingTelemetry()
    _bind_for_tool_module(monkeypatch, "durin.agent.tools.todos", sink)

    sessions = SessionManager(workspace=tmp_path)
    session = sessions.get_or_create("test")
    session.metadata = {}

    tool = TodoWriteTool(sessions=sessions)
    # TodoWriteTool implements ContextAware — bind a RequestContext via
    # set_context so the tool's session_key lookup hits "test".
    from durin.agent.tools.context import RequestContext
    tool.set_context(RequestContext(
        channel="cli", chat_id="test", session_key="test",
    ))
    await tool.execute(todos=[
        {"content": "do A", "status": "completed", "activeForm": "doing A"},
        {"content": "do B", "status": "in_progress", "activeForm": "doing B"},
        {"content": "do C", "status": "pending", "activeForm": "doing C"},
        {"content": "do D", "status": "pending", "activeForm": "doing D"},
    ])

    events = [e for e in sink.events if e[0] == "tool.todo_write"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload["total"] == 4
    assert payload["pending"] == 2
    assert payload["in_progress"] == 1
    assert payload["completed"] == 1
    assert payload["coerced_multiple_in_progress"] is False


@pytest.mark.asyncio
async def test_todo_write_coercion_recorded(monkeypatch, tmp_path):
    """When the model marks > 1 item in_progress, the runtime coerces
    extras back to pending — the event records that this happened so
    we can see how often models forget the contract."""
    from durin.agent.tools.todos import TodoWriteTool
    from durin.session.manager import SessionManager

    sink = _RecordingTelemetry()
    _bind_for_tool_module(monkeypatch, "durin.agent.tools.todos", sink)

    sessions = SessionManager(workspace=tmp_path)
    session = sessions.get_or_create("test")
    session.metadata = {}

    tool = TodoWriteTool(sessions=sessions)
    from durin.agent.tools.context import RequestContext
    tool.set_context(RequestContext(
        channel="cli", chat_id="test", session_key="test",
    ))
    await tool.execute(todos=[
        {"content": "A", "status": "in_progress", "activeForm": "doing A"},
        {"content": "B", "status": "in_progress", "activeForm": "doing B"},
        {"content": "C", "status": "in_progress", "activeForm": "doing C"},
    ])

    payload = [e for e in sink.events if e[0] == "tool.todo_write"][0][1]
    assert payload["coerced_multiple_in_progress"] is True
    # Only one stays in_progress; the rest pending.
    assert payload["in_progress"] == 1
    assert payload["pending"] == 2


# ---------------------------------------------------------------------------
# web_fetch is integration-heavy (real httpx mocking). We cover the
# emit-on-validation-error path here since that's the most common code
# path for security-driven errors and doesn't require mocking httpx.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_fetch_validation_error_emits_event(monkeypatch):
    """An unsupported URL (e.g. file://) fails validation BEFORE any
    HTTP call. We still want a telemetry event so dashboards see the
    blocked attempt."""
    from durin.agent.tools.web import WebFetchTool

    sink = _RecordingTelemetry()
    _bind_for_tool_module(monkeypatch, "durin.agent.tools.web", sink)

    cfg = MagicMock(use_jina_reader=False)
    tool = WebFetchTool(config=cfg, max_chars=10_000, user_agent="test")
    result = await tool.execute(url="file:///etc/passwd")

    # Validator should have blocked it (no network call needed).
    assert "URL validation" in result or "error" in str(result).lower()
    events = [e for e in sink.events if e[0] == "tool.web_fetch"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload["extractor"] == "validation"
    assert payload["error"] is True
    assert payload["is_image"] is False
