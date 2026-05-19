"""Tests for the ``sleep`` tool."""

from __future__ import annotations

import asyncio
import time

import pytest

from durin.agent.tools.sleep import SleepTool


@pytest.mark.asyncio
async def test_sleep_blocks_for_requested_duration():
    tool = SleepTool()
    start = time.monotonic()
    out = await tool.execute(seconds=0.1)
    elapsed = time.monotonic() - start

    assert "Slept" in out
    assert elapsed >= 0.08  # allow scheduler slack
    assert elapsed < 0.5    # but should not vastly overshoot


@pytest.mark.asyncio
async def test_sleep_rejects_negative_seconds():
    tool = SleepTool()
    out = await tool.execute(seconds=-1.0)
    assert "Error" in out


@pytest.mark.asyncio
async def test_sleep_requires_seconds_argument():
    tool = SleepTool()
    out = await tool.execute()
    assert "Error" in out
    assert "seconds" in out.lower()


@pytest.mark.asyncio
async def test_sleep_rejects_non_numeric():
    tool = SleepTool()
    out = await tool.execute(seconds="banana")
    assert "Error" in out


@pytest.mark.asyncio
async def test_sleep_clamps_to_ceiling(monkeypatch):
    """Asking for more than 300s should clamp to 300s, not error.

    We monkeypatch ``asyncio.sleep`` so the test does not actually wait
    five minutes; we just verify the value the tool passed in.
    """
    captured: list[float] = []

    async def fake_sleep(d: float) -> None:
        captured.append(d)

    monkeypatch.setattr("durin.agent.tools.sleep.asyncio.sleep", fake_sleep)

    tool = SleepTool()
    out = await tool.execute(seconds=600.0)

    assert captured == [300.0]
    assert "clamped" in out.lower()
    assert "300" in out


@pytest.mark.asyncio
async def test_sleep_zero_returns_immediately():
    tool = SleepTool()
    start = time.monotonic()
    out = await tool.execute(seconds=0)
    elapsed = time.monotonic() - start

    assert "Slept" in out
    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_sleep_is_cancellable():
    """A cancelled sleep should propagate CancelledError instead of
    silently returning a normal result."""
    tool = SleepTool()

    async def run():
        await tool.execute(seconds=10)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
