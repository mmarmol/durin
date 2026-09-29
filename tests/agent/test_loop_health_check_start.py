"""The first memory health check runs once the agent loop has started.

It used to run the moment the loop was built, which in the gateway is before
the channels are set up: a full scan of the memory store that took about
nine seconds at boot (against about one second later) while competing with
channel startup for the interpreter.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import MagicMock

from loguru import logger

import durin.memory.health_check as health_check
from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.config.schema import Config


def _app_config() -> Config:
    cfg = Config()
    cfg.memory.health_check.enabled = True
    cfg.memory.file_watcher.enabled = False
    cfg.catalog_refresh.enabled = False
    cfg.mcp_catalog_refresh.enabled = False
    return cfg


async def test_the_first_health_check_waits_for_the_loop_to_start(tmp_path, monkeypatch) -> None:
    events: list[str] = []

    class _Scheduler:
        def __init__(self, _checker, *, interval_seconds: int) -> None:
            self.interval_seconds = interval_seconds

        def start(self) -> None:
            events.append("health check started")

        def stop(self, timeout: float = 2.0) -> None:
            pass

    monkeypatch.setattr(health_check, "HealthCheckScheduler", _Scheduler)

    loop = AgentLoop(
        bus=MessageBus(), provider=MagicMock(), workspace=tmp_path,
        model="test-model", app_config=_app_config(),
    )

    async def _noop(*_a, **_kw) -> None:
        return None

    loop._connect_mcp = _noop  # type: ignore[method-assign]
    loop._warmup_memory_embedding = _noop  # type: ignore[method-assign]

    # Built with the loop, but not running yet.
    assert loop._memory_health_scheduler is not None
    assert events == []

    sink_id = logger.add(
        lambda m: events.append(m.record["message"]),
        level="INFO",
        filter=lambda r: r["message"] == "Agent loop started",
    )
    runner = asyncio.create_task(loop.run())
    try:
        async with asyncio.timeout(5):
            while "health check started" not in events:
                await asyncio.sleep(0.005)
    finally:
        logger.remove(sink_id)
        loop._running = False
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner
        loop.stop()

    assert events == ["Agent loop started", "health check started"]
