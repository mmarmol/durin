"""Tests for ChannelsRuntimeService (hot-start / hot-stop routes)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from durin.service.channels_runtime import (
    ChannelsRuntimeService,
    ChannelsRuntimeStatusQuery,
    ChannelStartCommand,
    ChannelStopCommand,
)
from durin.service.principal import Principal


def _principal():
    return Principal.local()


async def test_start_calls_manager():
    manager = MagicMock()
    manager.start_channel = AsyncMock()
    svc = ChannelsRuntimeService(channel_manager=manager)

    result = await svc.start(ChannelStartCommand(name="telegram"), _principal())

    assert result.ok is True
    assert result.error is None
    manager.start_channel.assert_awaited_once_with("telegram")


async def test_stop_calls_manager():
    manager = MagicMock()
    manager.stop_channel = AsyncMock()
    svc = ChannelsRuntimeService(channel_manager=manager)

    result = await svc.stop(ChannelStopCommand(name="slack"), _principal())

    assert result.ok is True
    assert result.error is None
    manager.stop_channel.assert_awaited_once_with("slack")


async def test_start_returns_error_when_manager_raises():
    manager = MagicMock()
    manager.start_channel = AsyncMock(side_effect=ValueError("Unknown channel: bad"))
    svc = ChannelsRuntimeService(channel_manager=manager)

    result = await svc.start(ChannelStartCommand(name="bad"), _principal())

    assert result.ok is False
    assert "Unknown channel" in (result.error or "")


async def test_stop_returns_error_when_manager_raises():
    manager = MagicMock()
    manager.stop_channel = AsyncMock(side_effect=RuntimeError("oops"))
    svc = ChannelsRuntimeService(channel_manager=manager)

    result = await svc.stop(ChannelStopCommand(name="telegram"), _principal())

    assert result.ok is False
    assert result.error is not None


async def test_start_no_manager_returns_error():
    svc = ChannelsRuntimeService(channel_manager=None)
    result = await svc.start(ChannelStartCommand(name="telegram"), _principal())
    assert result.ok is False
    assert "channel_manager not available" in (result.error or "")


async def test_stop_no_manager_returns_error():
    svc = ChannelsRuntimeService(channel_manager=None)
    result = await svc.stop(ChannelStopCommand(name="telegram"), _principal())
    assert result.ok is False
    assert "channel_manager not available" in (result.error or "")


def test_routes_registered():
    """Both routes must be declared on the service class."""
    from durin.service.registry import ROUTE_ATTR

    svc = ChannelsRuntimeService()
    start_spec = getattr(svc.start, ROUTE_ATTR, None)
    stop_spec = getattr(svc.stop, ROUTE_ATTR, None)

    assert start_spec is not None
    assert start_spec.path == "/api/v1/channels/start"
    assert start_spec.verb == "POST"

    assert stop_spec is not None
    assert stop_spec.path == "/api/v1/channels/stop"
    assert stop_spec.verb == "POST"


def test_parity_catalog_wiring():
    """catalog and wiring must register the same service names (includes channels_runtime)."""
    from durin.service.catalog import build_catalog_registry
    from durin.service.wiring import build_service_registry

    wiring = build_service_registry(
        config=None, session_manager=None, cron_service=None, bus=None
    )
    catalog = build_catalog_registry()

    wnames = {b.service_name for b in wiring.routes}
    cnames = {b.service_name for b in catalog.routes}

    assert wnames == cnames, (
        f"registry drift — catalog-only={cnames - wnames}, "
        f"wiring-only={wnames - cnames}"
    )
    assert "channels_runtime" in wnames
    assert any(b.spec.path == "/api/v1/channels/start" for b in wiring.routes)
    assert any(b.spec.path == "/api/v1/channels/stop" for b in wiring.routes)


async def test_runtime_status_reports_running_map():
    class _FakeManager:
        def get_status(self):
            return {
                "telegram": {"enabled": True, "running": True},
                "slack": {"enabled": True, "running": False},
            }

    svc = ChannelsRuntimeService(channel_manager=_FakeManager())
    res = await svc.runtime(ChannelsRuntimeStatusQuery(), Principal.local())
    assert res.running == {"telegram": True, "slack": False}


async def test_runtime_status_without_manager_is_empty():
    svc = ChannelsRuntimeService(channel_manager=None)
    res = await svc.runtime(ChannelsRuntimeStatusQuery(), Principal.local())
    assert res.running == {}
