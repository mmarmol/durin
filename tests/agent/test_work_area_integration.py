"""End-to-end integration of the per-session work area.

Unit tests construct the filesystem/exec tools directly and call
``set_context`` by hand. That bypasses two links the feature depends on in
production: (1) the real ``ToolLoader`` must register the tools as
``ContextAware`` instances, and (2) the agent loop's per-turn dispatch
(``AgentLoop._update_tool_context``) must reach them with the current
``RequestContext``. This test drives the real chain — config → loader →
the exact dispatch loop → real tool execution — and asserts the agent's
files land in ``work/<safe_key>/`` rather than the workspace root.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from durin.agent.tools.context import ContextAware, RequestContext, ToolContext
from durin.agent.tools.loader import ToolLoader
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import ToolsConfig


def _load_registry(workspace: Path) -> ToolRegistry:
    cfg = ToolsConfig()
    cfg.exec.enable = True
    ctx = ToolContext(config=cfg, workspace=str(workspace))
    registry = ToolRegistry()
    ToolLoader().load(ctx, registry)
    return registry


def _dispatch_context(registry: ToolRegistry, request_ctx: RequestContext) -> list[str]:
    """Replicate AgentLoop._update_tool_context's dispatch loop exactly."""
    dispatched: list[str] = []
    for name in registry.tool_names:
        tool = registry.get(name)
        if tool and isinstance(tool, ContextAware):
            tool.set_context(request_ctx)
            dispatched.append(name)
    return dispatched


def test_registered_file_and_exec_tools_are_context_aware(tmp_path):
    """The loop only dispatches set_context to ContextAware tools — the
    registered write/read/edit/exec tools must qualify, or the work area
    never activates in production."""
    registry = _load_registry(tmp_path)
    for name in ("write_file", "read_file", "edit_file", "exec"):
        tool = registry.get(name)
        assert tool is not None, f"{name} not registered"
        assert isinstance(tool, ContextAware), f"{name} is not ContextAware"


@pytest.mark.asyncio
async def test_agent_writes_land_in_session_work_dir(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = _load_registry(workspace)

    request_ctx = RequestContext(channel="cli", chat_id="42", session_key="cli:42")
    dispatched = _dispatch_context(registry, request_ctx)
    assert {"write_file", "read_file", "exec"} <= set(dispatched)

    await registry.get("write_file").execute(path="report.md", content="hello")
    await registry.get("exec").execute(command="echo script-output > out.txt")
    read_back = await registry.get("read_file").execute(path="report.md")

    work = workspace / "work" / "cli_42"
    assert (work / "report.md").read_text() == "hello"
    assert (work / "out.txt").exists()
    assert "hello" in str(read_back)
    # Nothing leaked to the workspace root.
    assert not (workspace / "report.md").exists()
    assert not (workspace / "out.txt").exists()
    assert [p.name for p in workspace.iterdir()] == ["work"]
