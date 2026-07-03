"""ChannelsRuntimeService — hot-start and hot-stop individual channels.

Enabling or disabling a channel via the settings UI writes the config file.
These two routes activate or deactivate the channel in the running gateway
immediately, without requiring a full gateway restart.
"""

from __future__ import annotations

from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, Query, Result


class ChannelsRuntimeStatusQuery(Query):
    pass


class ChannelsRuntimeStatusResult(Result):
    #: channel name -> whether its transport is actually alive right now
    running: dict[str, bool]


class ChannelStartCommand(Command):
    name: str


class ChannelStartResult(Result):
    ok: bool
    error: str | None = None


class ChannelStopCommand(Command):
    name: str


class ChannelStopResult(Result):
    ok: bool
    error: str | None = None


class ChannelsRuntimeService:
    """HTTP surface for hot-starting and hot-stopping individual channels."""

    def __init__(self, *, channel_manager: Any | None = None) -> None:
        self._channel_manager = channel_manager

    @route(
        "GET",
        "/api/v1/channels/runtime",
        scope=Scope.CONFIG_READ.value,
        request_model=ChannelsRuntimeStatusQuery,
        response_model=ChannelsRuntimeStatusResult,
        summary="Live per-channel running state (enabled in config ≠ actually running)",
    )
    async def runtime(
        self, query: ChannelsRuntimeStatusQuery, principal: Principal
    ) -> ChannelsRuntimeStatusResult:
        """Report which channels are actually alive in the running gateway."""
        principal.require(Scope.CONFIG_READ)
        if self._channel_manager is None:
            return ChannelsRuntimeStatusResult(running={})
        status = self._channel_manager.get_status()
        return ChannelsRuntimeStatusResult(
            running={name: bool(info.get("running")) for name, info in status.items()}
        )

    @route(
        "POST",
        "/api/v1/channels/start",
        scope=Scope.CONFIG_WRITE.value,
        request_model=ChannelStartCommand,
        response_model=ChannelStartResult,
        summary="Hot-start a channel in the running gateway (no restart required)",
    )
    async def start(self, cmd: ChannelStartCommand, principal: Principal) -> ChannelStartResult:
        """Activate channel *name* in the live gateway.

        The config file must already have the channel enabled (written by the
        settings UI before calling this endpoint).  The channel is instantiated
        and started; if already running the call is a no-op.
        """
        principal.require(Scope.CONFIG_WRITE)
        if self._channel_manager is None:
            return ChannelStartResult(ok=False, error="channel_manager not available")
        try:
            await self._channel_manager.start_channel(cmd.name)
            return ChannelStartResult(ok=True)
        except Exception as e:  # noqa: BLE001
            return ChannelStartResult(ok=False, error=str(e))

    @route(
        "POST",
        "/api/v1/channels/stop",
        scope=Scope.CONFIG_WRITE.value,
        request_model=ChannelStopCommand,
        response_model=ChannelStopResult,
        summary="Hot-stop a channel in the running gateway (no restart required)",
    )
    async def stop(self, cmd: ChannelStopCommand, principal: Principal) -> ChannelStopResult:
        """Deactivate channel *name* in the live gateway.

        The channel is stopped and removed from the active set; a subsequent
        ``start`` call will re-instantiate it with fresh config.
        """
        principal.require(Scope.CONFIG_WRITE)
        if self._channel_manager is None:
            return ChannelStopResult(ok=False, error="channel_manager not available")
        try:
            await self._channel_manager.stop_channel(cmd.name)
            return ChannelStopResult(ok=True)
        except Exception as e:  # noqa: BLE001
            return ChannelStopResult(ok=False, error=str(e))
