"""Stopping a foreground exec command stops everything the command started.

``bash -c "a && b"`` forks ``a`` as a child. Killing only the shell orphans
that child, which keeps running and holds the output pipes open. These tests
run real processes and look for survivors by a unique command line.
"""

import asyncio
import subprocess
import sys
import time
import uuid

import pytest

from durin.agent.tools.shell import ExecTool

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process groups"
)


def _unique_sleep() -> str:
    # A distinctive duration doubles as the pgrep marker.
    return f"sleep 97.{uuid.uuid4().int % 10**6:06d}"


def _survivors(marker: str) -> list[str]:
    """PIDs still matching *marker*, polled briefly so a SIGKILL can land."""
    deadline = time.monotonic() + 2.0
    while True:
        out = subprocess.run(
            ["pgrep", "-f", marker], capture_output=True, text=True
        ).stdout.split()
        if not out or time.monotonic() > deadline:
            return out
        time.sleep(0.05)


async def test_timeout_kills_the_whole_command_tree(tmp_path):
    marker = _unique_sleep()
    tool = ExecTool(working_dir=str(tmp_path))
    started = time.monotonic()
    result = await tool.execute(command=f"{marker} && echo never", timeout=1)
    elapsed = time.monotonic() - started
    assert "timed out" in result
    assert _survivors(marker) == []
    # The survivor used to hold the pipes open, stalling the reap for 5 s.
    assert elapsed < 4.0


async def test_cancel_kills_the_whole_command_tree(tmp_path):
    marker = _unique_sleep()
    tool = ExecTool(working_dir=str(tmp_path))
    task = asyncio.create_task(tool.execute(command=f"{marker} && echo never"))
    await asyncio.sleep(0.5)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started
    assert _survivors(marker) == []
    assert elapsed < 3.0
