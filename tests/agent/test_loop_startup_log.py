"""The agent loop logs how long connecting MCP servers took at startup.

A slow gateway boot is diagnosed from gateway.log's ``Startup:`` lines; the
MCP connect runs inside ``AgentLoop.run()``, so the loop logs it itself, just
before "Agent loop started".
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import MagicMock

import pytest
from loguru import logger

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus


async def _startup_lines(tmp_path, mcp_servers: dict) -> list[str]:
    loop = AgentLoop(
        bus=MessageBus(), provider=MagicMock(), workspace=tmp_path,
        model="test-model", mcp_servers=mcp_servers,
    )

    async def _connect() -> None:
        await asyncio.sleep(0.05)

    async def _noop(*_a, **_kw) -> None:
        return None

    loop._connect_mcp = _connect  # type: ignore[method-assign]
    loop._warmup_memory_embedding = _noop  # type: ignore[method-assign]
    lines: list[str] = []
    sink_id = logger.add(
        lambda m: lines.append(m.record["message"]),
        level="INFO",
        filter=lambda r: r["message"].startswith("Startup:") or r["message"] == "Agent loop started",
    )
    runner = asyncio.create_task(loop.run())
    try:
        async with asyncio.timeout(5):
            while "Agent loop started" not in lines:
                await asyncio.sleep(0.005)
        loop.stop_intake()
        await asyncio.wait_for(runner, 1)
    finally:
        logger.remove(sink_id)
        if not runner.done():
            runner.cancel()
        loop.stop()
    return lines


@pytest.mark.asyncio
async def test_run_logs_how_long_connecting_mcp_servers_took(tmp_path) -> None:
    lines = await _startup_lines(tmp_path, {"docs": {"command": "true"}})

    assert len(lines) == 2 and lines[1] == "Agent loop started", lines
    took = re.fullmatch(r"Startup: connecting MCP servers took (\d+\.\d\d)s", lines[0])
    assert took, lines
    assert float(took.group(1)) >= 0.05


@pytest.mark.asyncio
async def test_run_logs_no_mcp_line_without_mcp_servers(tmp_path) -> None:
    assert await _startup_lines(tmp_path, {}) == ["Agent loop started"]
