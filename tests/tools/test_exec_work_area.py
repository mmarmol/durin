"""Tests for exec tool defaulting its cwd to the per-session work area."""

from __future__ import annotations

import pytest

from durin.agent.tools.context import RequestContext
from durin.agent.tools.shell import ExecTool


@pytest.mark.asyncio
async def test_exec_writes_land_in_work_dir(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    tool.set_context(RequestContext(channel="cli", chat_id="1", session_key="cli:1"))
    await tool.execute(command="echo hi > out.txt")
    assert (tmp_path / "work" / "cli_1" / "out.txt").read_text().strip() == "hi"


@pytest.mark.asyncio
async def test_exec_no_context_uses_workspace(tmp_path):
    """Without a session context, cwd must be the workspace root."""
    tool = ExecTool(working_dir=str(tmp_path))
    await tool.execute(command="echo no-ctx > out.txt")
    assert (tmp_path / "out.txt").read_text().strip() == "no-ctx"


@pytest.mark.asyncio
async def test_exec_explicit_working_dir_overrides_work_dir(tmp_path):
    """An explicit working_dir arg still wins over the per-session default."""
    custom = tmp_path / "custom"
    custom.mkdir()
    tool = ExecTool(working_dir=str(tmp_path))
    tool.set_context(RequestContext(channel="cli", chat_id="1", session_key="cli:1"))
    await tool.execute(command="echo explicit > out.txt", working_dir=str(custom))
    assert (custom / "out.txt").read_text().strip() == "explicit"
    # Work dir should NOT contain the file
    assert not (tmp_path / "work" / "cli_1" / "out.txt").exists()


@pytest.mark.asyncio
async def test_exec_work_dir_created_on_demand(tmp_path):
    """The work directory is created if it doesn't exist when executing."""
    tool = ExecTool(working_dir=str(tmp_path))
    tool.set_context(RequestContext(channel="cli", chat_id="2", session_key="cli:2"))
    expected_dir = tmp_path / "work" / "cli_2"
    assert not expected_dir.exists()
    await tool.execute(command="echo hello")
    assert expected_dir.exists()
