"""Tests for the process tool (list/poll/kill)."""

from __future__ import annotations

import asyncio
import sys

import pytest

from durin.agent.tools.process_registry import get_process_registry
from durin.agent.tools.process_tool import ProcessTool

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups are POSIX-only in v1",
)


@pytest.fixture(autouse=True)
async def _clean_registry():
    reg = get_process_registry()
    await reg.shutdown()
    reg._running.clear()
    reg._finished.clear()
    yield
    await reg.shutdown()
    reg._running.clear()
    reg._finished.clear()


def _env() -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}


@pytest.mark.asyncio
async def test_poll_shows_output_tail(tmp_path):
    reg = get_process_registry()
    sess = await reg.spawn("echo poll-me", cwd=str(tmp_path), env=_env())
    for _ in range(50):
        if sess.exited:
            break
        await asyncio.sleep(0.1)
    tool = ProcessTool()
    result = await tool.execute(action="poll", id=sess.id)
    assert "exited" in result
    assert "poll-me" in result


@pytest.mark.asyncio
async def test_list_shows_running(tmp_path):
    reg = get_process_registry()
    sess = await reg.spawn("sleep 30", cwd=str(tmp_path), env=_env())
    tool = ProcessTool()
    result = await tool.execute(action="list")
    assert sess.id in result
    assert "running" in result


@pytest.mark.asyncio
async def test_kill_stops_process(tmp_path):
    reg = get_process_registry()
    sess = await reg.spawn("sleep 300", cwd=str(tmp_path), env=_env())
    tool = ProcessTool()
    result = await tool.execute(action="kill", id=sess.id)
    assert "killed" in result.lower() or sess.id in result
    for _ in range(50):
        if sess.exited:
            break
        await asyncio.sleep(0.1)
    assert sess.exited


@pytest.mark.asyncio
async def test_poll_requires_id():
    tool = ProcessTool()
    result = await tool.execute(action="poll")
    assert "Error" in result


@pytest.mark.asyncio
async def test_unknown_id():
    tool = ProcessTool()
    result = await tool.execute(action="poll", id="proc_missing")
    assert "not found" in result


@pytest.mark.asyncio
async def test_list_empty():
    tool = ProcessTool()
    result = await tool.execute(action="list")
    assert "No background processes" in result
