"""HealthService — extras status/install/restart and log viewer.

Extracted from ``durin/channels/websocket.py``
(``_handle_extras_status`` / ``_handle_extras_ensure`` /
``_handle_extras_restart`` / ``_handle_logs_list``) in SP1.

System-op split (mirrors ``cron.run``):
- ``extras_ensure``: the service validates the feature and performs the
  ``ensure_extra`` install (domain logic). Whether a restart is needed and
  was requested is returned in the result; the *actual* subprocess spawn
  (``_spawn_gateway_restart``) stays in the websocket shim (loop/process
  concern).
- ``extras_restart``: the service just returns ``{"restarting": True}``; the
  shim does the subprocess spawn.

Escape hatches:
- ``LogsListResult``: ``lines``, ``facets``, and ``next_cursor`` carry
  ``dict[str, Any]`` / ``list[Any]`` payloads whose sub-structure comes
  directly from ``durin.logs.reader`` — not modelled further here (open by
  design; tighten if the shape is ever frozen).
"""

from __future__ import annotations

import asyncio
from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    Query,
    Result,
    ValidationFailedError,
)

# ---------------------------------------------------------------------------
# DTOs — status
# ---------------------------------------------------------------------------


class RuntimeStatusQuery(Query):
    """No inputs — one aggregate snapshot."""


class RuntimeStatusResult(Result):
    version: str
    uptime_s: float | None = None
    # Per-channel {name, enabled, running}; only channels that are enabled
    # in config or live in the running manager are listed.
    channels: list[dict[str, Any]] = []
    # CronService.status() passthrough: {enabled, jobs, next_wake_at_ms};
    # None when no scheduler is wired (shim registry, tests).
    cron: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# DTOs — memory diagnostics
# ---------------------------------------------------------------------------


class MemoryDiagnosticsQuery(Query):
    """No inputs — one process-footprint snapshot."""


class MemoryDiagnosticsResult(Result):
    # Resident set of the gateway process and of its child processes
    # (embedding pool workers, spawned helpers), in MB.
    rss_mb: float
    children_mb: float
    threads: int
    # gc generation sizes — a monotonically ballooning gen2 is the classic
    # signature of Python-side retention.
    gc_counts: list[int] = []
    # Host context so a reader can judge headroom without a second call.
    total_mb: float
    available_mb: float


# ---------------------------------------------------------------------------
# DTOs — extras_status
# ---------------------------------------------------------------------------


class ExtrasStatusQuery(Query):
    feature: str


class ExtrasStatusResult(Result):
    present: bool
    extra: str
    approx_size: str
    needs_restart: bool
    label: str


# ---------------------------------------------------------------------------
# DTOs — extras_ensure
# ---------------------------------------------------------------------------


class ExtrasEnsureCommand(Command):
    feature: str
    restart: bool = False


class ExtrasEnsureResult(Result):
    status: str
    needs_restart: bool
    message: str
    restarting: bool = False


# ---------------------------------------------------------------------------
# DTOs — extras_restart
# ---------------------------------------------------------------------------


class ExtrasRestartCommand(Command):
    """No inputs — unconditional restart."""


class ExtrasRestartResult(Result):
    restarting: bool


# ---------------------------------------------------------------------------
# DTOs — logs_list (escape-hatch result)
# ---------------------------------------------------------------------------


class LogsListQuery(Query):
    source: str = "gateway"
    q: str | None = None
    before_ts: float | None = None
    window_hours: float | None = 24.0
    limit: int = 200
    level: list[str] = []
    channel: list[str] = []
    session: list[str] = []
    type: list[str] = []


class LogsListResult(Result):
    lines: list[Any]
    facets: dict[str, Any]
    next_cursor: Any
    scanned_through_ts: Any
    has_more: bool


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class HealthService:
    """System-health service: runtime status, extras lifecycle + log viewer.

    ``channel_manager`` and ``cron_service`` are optional live-gateway deps
    used by the ``status`` route; surfaces without them (the websocket
    channel's shim registry, tests) report config-only data.
    """

    def __init__(
        self,
        *,
        channel_manager: Any | None = None,
        cron_service: Any | None = None,
    ) -> None:
        self._channel_manager = channel_manager
        self._cron_service = cron_service

    @route(
        "GET",
        "/api/v1/status",
        scope=Scope.SYSTEM_READ.value,
        request_model=RuntimeStatusQuery,
        response_model=RuntimeStatusResult,
        summary="One-call runtime snapshot: version, uptime, channels, cron",
    )
    async def status(
        self, query: RuntimeStatusQuery, principal: Principal
    ) -> RuntimeStatusResult:
        """Aggregate runtime state for ``durin status`` and other clients.

        One HTTP call answers "what is this gateway running right now":
        package version, process uptime, per-channel enabled/running state,
        and the cron scheduler summary.
        """
        principal.require(Scope.SYSTEM_READ)
        from durin import __version__
        from durin.config.loader import load_config
        from durin.utils.process_runtime import uptime_s as process_uptime

        uptime_s = process_uptime()

        # Channels: enabled comes from config; running from the live manager.
        channels: list[dict[str, Any]] = []
        try:
            config = load_config()
            extra = getattr(config.channels, "__pydantic_extra__", None) or {}
        except Exception:  # noqa: BLE001 — config errors leave channels empty
            extra = {}
        running_map: dict[str, Any] = (
            getattr(self._channel_manager, "channels", None) or {}
            if self._channel_manager is not None
            else {}
        )
        names = sorted(set(extra) | set(running_map))
        for name in names:
            section = extra.get(name)
            enabled = (
                bool(section.get("enabled"))
                if isinstance(section, dict)
                else bool(getattr(section, "enabled", False))
            )
            inst = running_map.get(name)
            running = bool(getattr(inst, "is_running", False)) if inst is not None else False
            if not enabled and not running:
                continue
            channels.append({"name": name, "enabled": enabled, "running": running})

        cron: dict[str, Any] | None = None
        if self._cron_service is not None:
            try:
                cron = self._cron_service.status()
            except Exception:  # noqa: BLE001 — cron summary is best-effort
                cron = None

        return RuntimeStatusResult(
            version=__version__,
            uptime_s=uptime_s,
            channels=channels,
            cron=cron,
        )

    @route(
        "GET",
        "/api/v1/diagnostics/memory",
        scope=Scope.SYSTEM_READ.value,
        request_model=MemoryDiagnosticsQuery,
        response_model=MemoryDiagnosticsResult,
        summary="Gateway memory footprint: RSS, children, threads, gc, host headroom",
    )
    async def memory_diagnostics(
        self, query: MemoryDiagnosticsQuery, principal: Principal
    ) -> MemoryDiagnosticsResult:
        """Live footprint of the gateway process, on demand.

        The 2026-07-18 incident review found the production gateway at 2GB
        resident with no way to ask it why; this route (with the periodic
        ``gateway.memory`` telemetry) is the first-class instrument for that
        question.
        """
        principal.require(Scope.SYSTEM_READ)
        from durin.utils.process_tree import memory_snapshot

        snap = await asyncio.to_thread(memory_snapshot)
        return MemoryDiagnosticsResult(**snap)

    @route(
        "GET",
        "/api/v1/extras/status",
        scope=Scope.SYSTEM_READ.value,
        request_model=ExtrasStatusQuery,
        response_model=ExtrasStatusResult,
        summary="Is a pip extra installed + what would installing it cost",
    )
    async def extras_status(
        self, query: ExtrasStatusQuery, principal: Principal
    ) -> ExtrasStatusResult:
        principal.require(Scope.SYSTEM_READ)
        from durin.extras import REGISTRY, _module_present

        fe = REGISTRY.get(query.feature)
        if fe is None:
            raise ValidationFailedError(
                f"unknown feature '{query.feature}'",
                details={"feature": query.feature},
            )
        return ExtrasStatusResult(
            present=_module_present(fe.module),
            extra=fe.extra,
            approx_size=fe.approx_size,
            needs_restart=fe.needs_restart,
            label=fe.label,
        )

    @route(
        "POST",
        "/api/v1/extras/ensure",
        scope=Scope.SYSTEM_WRITE.value,
        request_model=ExtrasEnsureCommand,
        response_model=ExtrasEnsureResult,
        summary="Install a pip extra (off-loop); signal whether a restart is needed",
    )
    async def extras_ensure(
        self, cmd: ExtrasEnsureCommand, principal: Principal
    ) -> ExtrasEnsureResult:
        """Validate + install; the shim calls ``_spawn_gateway_restart`` when
        ``result.restarting is True``."""
        principal.require(Scope.SYSTEM_WRITE)
        from durin.config.loader import load_config
        from durin.extras import REGISTRY, ensure_extra

        if cmd.feature not in REGISTRY:
            raise ValidationFailedError(
                f"unknown feature '{cmd.feature}'",
                details={"feature": cmd.feature},
            )
        res = await asyncio.to_thread(ensure_extra, cmd.feature, config=load_config())
        do_restart = res.status == "installed" and cmd.restart and res.needs_restart
        return ExtrasEnsureResult(
            status=res.status,
            needs_restart=res.needs_restart,
            message=res.message,
            restarting=do_restart,
        )

    @route(
        "POST",
        "/api/v1/extras/restart",
        scope=Scope.SYSTEM_WRITE.value,
        request_model=ExtrasRestartCommand,
        response_model=ExtrasRestartResult,
        summary="Unconditionally restart the gateway daemon",
    )
    async def extras_restart(
        self, cmd: ExtrasRestartCommand, principal: Principal
    ) -> ExtrasRestartResult:
        """Signal to the shim that a restart should be spawned.

        The actual ``_spawn_gateway_restart()`` subprocess spawn stays in the
        websocket shim (loop/process concern).
        """
        principal.require(Scope.SYSTEM_WRITE)
        return ExtrasRestartResult(restarting=True)

    @route(
        "GET",
        "/api/v1/logs",
        scope=Scope.SYSTEM_READ.value,
        request_model=LogsListQuery,
        response_model=LogsListResult,
        summary="Read JSONL log segments (gateway or telemetry) with pagination",
    )
    async def logs_list(
        self, query: LogsListQuery, principal: Principal
    ) -> LogsListResult:
        principal.require(Scope.SYSTEM_READ)
        from pathlib import Path

        from durin.cli.gateway_daemon import daemon_logs_path
        from durin.logs.reader import LogQuery, compute_facets, read_page

        if query.source == "telemetry":
            directory = Path.home() / ".cache" / "durin" / "telemetry"
        else:
            directory = daemon_logs_path().parent

        filters: dict[str, set[str]] = {}
        for key in ("level", "channel", "session", "type"):
            vals = getattr(query, key)
            if vals:
                filters[key] = set(vals)

        log_query = LogQuery(
            source="telemetry" if query.source == "telemetry" else "gateway",
            q=query.q or None,
            before_ts=query.before_ts,
            window_hours=query.window_hours,
            limit=max(1, min(query.limit, 1000)),
            filters=filters,
        )
        try:
            page = read_page(directory, log_query)
            facets = compute_facets(directory, log_query.source)
        except Exception as exc:  # noqa: BLE001
            from durin.service.types import UnavailableError

            raise UnavailableError(f"log read failed: {exc}") from exc
        return LogsListResult(
            lines=page.lines,
            facets=facets,
            next_cursor=page.next_cursor,
            scanned_through_ts=page.scanned_through_ts,
            has_more=page.has_more,
        )
