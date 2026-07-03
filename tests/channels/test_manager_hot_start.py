"""Tests for ChannelManager.start_channel / stop_channel (hot-start/stop)."""
from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

import durin.channels.manager as mgr


def _make_manager():
    """Build a minimal ChannelManager via __new__ (skips __init__)."""
    m = mgr.ChannelManager.__new__(mgr.ChannelManager)
    m.channels = {}
    m.config = types.SimpleNamespace(
        channels=types.SimpleNamespace(
            transcription_provider="openai",
            transcription_language="en",
            send_progress=True,
            send_tool_hints=True,
            show_reasoning=False,
        ),
        providers=types.SimpleNamespace(
            openai=types.SimpleNamespace(api_key="", api_base=""),
        ),
        voice=None,
    )
    m.transcription = None
    m.speech_synthesis = None
    m._session_manager = None
    m._webui_runtime_model_name = None
    m._webui_runtime_model_preset = None
    m._cron_service = None
    return m


class _FakeChannel:
    name = "fake"
    display_name = "Fake"

    def __init__(self, section, bus, **kwargs):
        self.section = section
        self.started = False
        self.stopped = False
        self.transcription_provider = None
        self.transcription_api_key = None
        self.transcription_api_base = None
        self.transcription_language = None
        self.transcription = None
        self.speech_synthesis = None
        self.voice_config = None
        self.send_progress = True
        self.send_tool_hints = True
        self.show_reasoning = False
        self.is_running = False

    async def start(self):
        self.started = True
        self.is_running = True

    async def stop(self):
        self.stopped = True
        self.is_running = False


async def test_start_channel_instantiates_and_starts(monkeypatch):
    m = _make_manager()
    # Provide a fake "fake" channel section in config
    m.config.channels.fake = types.SimpleNamespace(enabled=True)

    fake_instance = None

    def _make_fake(name):
        nonlocal fake_instance
        fake_instance = _FakeChannel(None, None)
        return fake_instance

    monkeypatch.setattr(m, "_make_channel", _make_fake)

    fake_config = MagicMock()
    with patch("durin.config.loader.load_config", return_value=fake_config):
        await m.start_channel("fake")

    assert "fake" in m.channels
    assert fake_instance is not None
    assert fake_instance.started is True
    # config reloaded
    assert m.config is fake_config


async def test_start_channel_noop_if_already_running(monkeypatch):
    m = _make_manager()
    existing = _FakeChannel(None, None)
    existing.is_running = True
    m.channels["fake"] = existing

    make_called = []
    monkeypatch.setattr(m, "_make_channel", lambda n: make_called.append(n))

    # Early return before load_config is reached — no patch needed
    await m.start_channel("fake")

    assert make_called == []  # _make_channel not called
    assert m.channels["fake"] is existing


async def test_start_channel_raises_on_unknown(monkeypatch):
    m = _make_manager()

    monkeypatch.setattr(m, "_make_channel", lambda n: None)

    fake_config = MagicMock()
    with patch("durin.config.loader.load_config", return_value=fake_config):
        with pytest.raises(ValueError, match="Unknown or unconfigured"):
            await m.start_channel("nonexistent")

    assert "nonexistent" not in m.channels


async def test_start_channel_removes_from_channels_on_start_failure(monkeypatch):
    m = _make_manager()

    bad_channel = _FakeChannel(None, None)

    async def _failing_start():
        raise RuntimeError("Bot connection refused")
    bad_channel.start = _failing_start

    monkeypatch.setattr(m, "_make_channel", lambda n: bad_channel)

    fake_config = MagicMock()
    with patch("durin.config.loader.load_config", return_value=fake_config):
        with pytest.raises(RuntimeError, match="Bot connection refused"):
            await m.start_channel("fake")

    assert "fake" not in m.channels


async def test_stop_channel_stops_and_removes():
    m = _make_manager()
    ch = _FakeChannel(None, None)
    m.channels["fake"] = ch

    await m.stop_channel("fake")

    assert ch.stopped is True
    assert "fake" not in m.channels


async def test_stop_channel_noop_if_not_running():
    m = _make_manager()
    # "fake" not in channels
    await m.stop_channel("fake")  # must not raise


async def test_stop_channel_removes_even_on_stop_error():
    m = _make_manager()
    ch = _FakeChannel(None, None)

    async def _failing_stop():
        raise RuntimeError("network gone")
    ch.stop = _failing_stop

    m.channels["fake"] = ch

    # Should not raise; exception is swallowed
    await m.stop_channel("fake")

    assert "fake" not in m.channels


async def test_start_channel_restarts_dead_registered_channel(monkeypatch):
    """A channel whose start() bailed at boot stays in `channels` with
    is_running False; hot-start must rebuild it, not no-op forever."""
    m = _make_manager()
    m._background_tasks = set()
    dead = _FakeChannel(None, None)
    dead.is_running = False
    m.channels["fake"] = dead
    m.config.channels.fake = types.SimpleNamespace(enabled=True)

    fresh = _FakeChannel(None, None)
    monkeypatch.setattr(m, "_make_channel", lambda n: fresh)

    fake_config = MagicMock()
    fake_config.channels.fake = types.SimpleNamespace(enabled=True)
    with patch("durin.config.loader.load_config", return_value=fake_config):
        await m.start_channel("fake")

    assert dead.stopped is True
    assert m.channels["fake"] is fresh
    assert fresh.started is True


async def test_start_channel_surfaces_channel_that_bails_without_running(monkeypatch):
    """start() returning immediately without coming up (e.g. missing tokens)
    must be reported as a failure, not a phantom success."""
    m = _make_manager()
    m._background_tasks = set()
    m.config.channels.fake = types.SimpleNamespace(enabled=True)

    bailing = _FakeChannel(None, None)

    async def _bail():
        return  # logs-an-error-and-returns pattern; is_running stays False
    bailing.start = _bail

    monkeypatch.setattr(m, "_make_channel", lambda n: bailing)

    fake_config = MagicMock()
    fake_config.channels.fake = types.SimpleNamespace(enabled=True)
    with patch("durin.config.loader.load_config", return_value=fake_config):
        with pytest.raises(ValueError, match="did not start"):
            await m.start_channel("fake")

    assert "fake" not in m.channels


async def test_start_channel_parks_long_running_start(monkeypatch):
    """A start() that runs its keepalive loop inline (Slack) must not hang the
    hot-start caller — it gets parked as a background task."""
    import asyncio

    monkeypatch.setattr(mgr, "_HOT_START_FAIL_FAST_WINDOW_S", 0.05)
    m = _make_manager()
    m._background_tasks = set()
    m.config.channels.fake = types.SimpleNamespace(enabled=True)

    release = asyncio.Event()
    long_running = _FakeChannel(None, None)

    async def _keepalive():
        long_running.is_running = True
        await release.wait()
    long_running.start = _keepalive

    monkeypatch.setattr(m, "_make_channel", lambda n: long_running)

    fake_config = MagicMock()
    fake_config.channels.fake = types.SimpleNamespace(enabled=True)
    with patch("durin.config.loader.load_config", return_value=fake_config):
        await m.start_channel("fake")  # must return promptly

    assert m.channels["fake"] is long_running
    assert len(m._background_tasks) == 1
    release.set()
    await asyncio.sleep(0)
