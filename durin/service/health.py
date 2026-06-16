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
  directly from ``durin.logs.reader`` — not modelled further here (SP3 can
  tighten if needed).
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
    """System-health service: extras lifecycle + log viewer.

    No constructor dependencies — all reads go to the REGISTRY and the log
    files on disk directly.
    """

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
