"""Tests for exec(background=true)."""

from __future__ import annotations

import sys
import time

import pytest

from durin.agent.tools.process_registry import get_process_registry
from durin.agent.tools.shell import ExecTool

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups are POSIX-only in v1",
)


@pytest.fixture(autouse=True)
async def _clean_registry():
    yield
    await get_process_registry().shutdown()


@pytest.mark.asyncio
async def test_background_returns_immediately_with_proc_id(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    start = time.monotonic()
    result = await tool.execute(command="sleep 30", background=True)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0            # did not wait for the sleep
    assert "proc_" in result
    assert "process" in result      # mentions the polling tool


@pytest.mark.asyncio
async def test_background_respects_deny_patterns(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    result = await tool.execute(command="rm -rf /", background=True)
    assert "blocked by deny pattern" in result


@pytest.mark.asyncio
async def test_background_process_is_tracked(tmp_path):
    tool = ExecTool(working_dir=str(tmp_path))
    result = await tool.execute(command="echo tracked-output", background=True)
    proc_id = next(tok for tok in result.split() if tok.startswith("proc_"))
    proc_id = proc_id.strip(".,:;)'\"")
    reg = get_process_registry()
    assert reg.get(proc_id) is not None
