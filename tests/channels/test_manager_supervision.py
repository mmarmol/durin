from __future__ import annotations

import asyncio

import pytest

from durin.channels.base import BaseChannel
from durin.channels.manager import ChannelManager


class _CrashOnceChannel(BaseChannel):
    name = "crashonce"

    def __init__(self) -> None:
        # Bypass BaseChannel.__init__ plumbing; supervision only touches start().
        self.starts = 0
        self._running = False

    async def start(self) -> None:
        self.starts += 1
        if self.starts == 1:
            raise RuntimeError("boom")

    async def stop(self) -> None:
        pass

    async def send(self, msg) -> None:
        pass


@pytest.mark.asyncio
async def test_supervisor_restarts_crashed_channel(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("durin.channels.manager.asyncio.sleep", fake_sleep)
    manager = ChannelManager.__new__(ChannelManager)
    channel = _CrashOnceChannel()
    await manager._start_channel("crashonce", channel)
    assert channel.starts == 2          # crashed once, restarted, clean return ends loop
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_supervisor_does_not_restart_clean_return() -> None:
    manager = ChannelManager.__new__(ChannelManager)
    channel = _CrashOnceChannel()
    channel.starts = 10                  # start() will return cleanly
    await manager._start_channel("crashonce", channel)
    assert channel.starts == 11
