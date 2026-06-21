"""Per-server MCP supervision: a connection that owns a replaceable session.

SP-2. Each MCPServerConnection runs a single asyncio.Task that enters AND
exits the transport + ClientSession async-context in the same task, so the
anyio cancel scopes created by the SDK transport are torn down where they
were created (fixing the cross-task swallow at loop.py:1560). The connection
holds the LIVE session and replaces it on reconnect; wrappers re-resolve
``connection.session`` per call so a reconnect doesn't strand in-flight
tool-call IDs.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from loguru import logger

from durin.agent.tools.mcp import (
    MCPPromptWrapper,
    MCPResourceWrapper,
    MCPToolWrapper,
    _disable_output_schema_validation,
    _normalize_windows_stdio_command,
    _probe_http_url,
    _sanitize_name,
)
from durin.agent.tools.registry import ToolRegistry
from durin.config.paths import get_logs_dir

_mcp_stderr_handle = None  # module-level shared handle

_MCP_MAX_REDIRECTS = 5  # cap redirect chains on MCP HTTP transports (DoS / redirect-loop guard)


def _mcp_stderr_log():
    """Shared append handle for MCP-server stderr so server banners don't
    corrupt the TUI (the SDK defaults errlog to sys.stderr). Line-buffered,
    errors replaced; falls back to sys.stderr if the file can't be opened."""
    global _mcp_stderr_handle
    if _mcp_stderr_handle is not None:
        return _mcp_stderr_handle
    try:
        path = get_logs_dir() / "mcp-stderr.log"
        _mcp_stderr_handle = open(path, "a", buffering=1, errors="replace")  # noqa: SIM115
    except Exception:  # noqa: BLE001
        _mcp_stderr_handle = sys.stderr
    return _mcp_stderr_handle


@dataclass
class _ConnDown:
    """Sentinel: the connection can't service the call right now.

    ``message`` is the model-facing text the wrapper returns verbatim
    (breaker-open guidance or 'not connected'). Carries no payload.
    """

    message: str


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


_SESSION_EXPIRED_MARKERS = (
    "invalid or expired session", "expired session", "session expired",
    "session not found", "unknown session", "session terminated",
    "closedresourceerror", "closed resource", "transport is closed",
    "connection closed", "broken pipe", "end of file",
)


def _resolve_secret_map(values: dict[str, str] | None) -> dict[str, str] | None:
    """Resolve any ``${secret:NAME}`` references to plaintext at the point of use.

    MCP ``env`` / ``headers`` may hold secret-store references written by the
    discovery install flow; the plaintext is materialised only here, right before
    the transport spawns, and never persists in config or logs.
    """
    if not values:
        return values
    from durin.security.secrets import resolve_secret

    return {k: resolve_secret(v) for k, v in values.items()}


def _is_session_expired_error(exc: BaseException) -> bool:
    if isinstance(exc, InterruptedError):
        return False
    msg = str(exc).lower()
    return bool(msg) and any(m in msg for m in _SESSION_EXPIRED_MARKERS)


def _auth_error_types() -> tuple:
    """Auth/401 exception types. SP-4 owns OAuth recovery; for now this only
    classifies a 401 as non-retryable at initial connect (a clear seam)."""
    types: list = []
    try:
        from mcp.client.auth import OAuthFlowError, OAuthTokenError
        types.extend([OAuthFlowError, OAuthTokenError])
    except ImportError:
        pass
    try:
        import httpx
        types.append(httpx.HTTPStatusError)
    except ImportError:
        pass
    return tuple(types)


def _is_auth_error(exc: BaseException) -> bool:
    types = _auth_error_types()
    if not types or not isinstance(exc, types):
        return False
    try:
        import httpx
        if isinstance(exc, httpx.HTTPStatusError):
            return getattr(exc.response, "status_code", None) == 401
    except ImportError:
        pass
    return True


def _is_transient_conn(exc: BaseException) -> bool:
    from durin.agent.tools.mcp import _is_transient
    return _is_transient(exc)


def _is_timeout_error(exc: BaseException) -> bool:
    return isinstance(exc, asyncio.TimeoutError) or "timed out while waiting" in str(exc).lower()


# Reconnect / backoff constants (monkeypatchable in tests).
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
_MAX_INITIAL_CONNECT_RETRIES = 3
_MAX_RECONNECT_RETRIES = 5
# Upper bound on the initial connect. A server can hang here indefinitely —
# most often an OAuth server whose interactive-auth abort the MCP SDK swallows,
# leaving the HTTP request pending forever so _ready is never set. Since
# connect_mcp_servers connects sequentially and run() awaits _connect_mcp before
# its consume loop, an unbounded wait lets one un-authed server brick every
# turn. Generous enough for legitimate cold starts (stdio npx, SSE handshakes).
_CONNECT_TIMEOUT = 30.0
_KEEPALIVE_INTERVAL = 180.0
_KEEPALIVE_TIMEOUT = 30.0

# Circuit-breaker constants (monkeypatchable in tests).
_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN_SEC = 60.0


class MCPServerConnection:
    """Owns one MCP server's session lifecycle in a dedicated task."""

    def __init__(
        self,
        name: str,
        cfg: Any,
        registry: ToolRegistry,
        defer_cb: Callable[[], None] | None = None,
        workspace: str | None = None,
        sampling_runner: Any | None = None,
    ) -> None:
        self.name = name
        self._cfg = cfg
        self._registry = registry
        self._defer_cb = defer_cb
        self._workspace = workspace
        self._sampling_runner = sampling_runner

        self.session: Any | None = None
        self.initialize_result: Any | None = None

        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._reconnect_event = asyncio.Event()
        self._error: BaseException | None = None
        self._registered_names: list[str] = []

        self._rpc_lock = asyncio.Lock()

        # Breaker state.
        self._error_count = 0
        self._breaker_opened_at: float | None = None

        # Keepalive interval (config-overridable for tests).
        self._keepalive_interval = float(
            getattr(cfg, "keepalive_interval", _KEEPALIVE_INTERVAL)
        )

        # OAuth provider (SP-4): built once per connection so refresh/DCR
        # state persists across reconnects. None unless cfg marks oauth.
        self._oauth_provider = self._build_oauth_provider()

        # list_changed refresh state.
        self._refresh_lock = asyncio.Lock()
        self._pending_refresh: set[asyncio.Task] = set()
        self._refresh_generation = 0

        # SP-5b: injection-scan findings (deterministic test hook; also drives WARNING log).
        self._injection_findings: list[tuple[str, list[str]]] = []

    def _build_oauth_provider(self):
        """Build the SDK OAuthClientProvider when cfg.oauth is set, else None.

        Built once in __init__ so DCR/refresh state persists across reconnects
        within the same process. Headless: refuses to open a browser; the user
        must run `durin mcp login <server>` to obtain tokens.
        """
        cfg = self._cfg
        oauth = getattr(cfg, "oauth", None)
        if not oauth:
            return None
        if not getattr(cfg, "url", ""):
            logger.warning("MCP '{}': oauth set but no url; ignoring", self.name)
            return None
        from durin.agent.tools.mcp_oauth import build_oauth_provider
        return build_oauth_provider(self.name, cfg, headless=True)

    # ----- SP-5 security helpers -----

    def _build_http_client(self, extra_kwargs: dict) -> "Any":
        """Build the MCP transport's httpx client.

        SSRF posture: unless this server is explicitly opted out via
        ``allow_private_url`` (or an SDK proxy takes over egress), the client
        is built through ``ssrf_safe_async_client`` so every request — initial
        and each redirect hop — is resolved, validated against the blocked
        private/loopback/link-local/metadata networks, and pinned to the
        validated IP (closing the DNS-rebinding TOCTOU). The global
        ``tools.ssrf_whitelist`` (already applied at config load) still
        governs which private ranges are exempt.
        """
        import httpx

        from durin.security.network import ssrf_safe_async_client

        if getattr(self._cfg, "allow_private_url", False):
            return httpx.AsyncClient(**extra_kwargs)
        return ssrf_safe_async_client(**extra_kwargs)

    def _sse_client_factory(self):
        """Return the httpx client factory for the SSE transport.

        Extracted so it is independently testable and so _open_sse stays
        focused on the transport lifecycle.
        """
        cfg = self._cfg
        provider = self._oauth_provider

        def factory(headers=None, timeout=None, auth=None):
            merged = {
                "Accept": "application/json, text/event-stream",
                **(_resolve_secret_map(cfg.headers) or {}),
                **(headers or {}),
            }
            return self._build_http_client(
                dict(
                    headers=merged or None,
                    follow_redirects=True,
                    max_redirects=_MCP_MAX_REDIRECTS,
                    timeout=timeout,
                    auth=provider or auth,  # SP-4: provider drives auth when set
                )
            )

        return factory

    def _scan_metadata(self, kind: str, item_name: str, *texts: object) -> None:
        """Warn (never block) on structural injection markers in untrusted
        MCP metadata. ``texts`` are the description/name fields to scan."""
        from durin.agent.tools.mcp_security import scan_injection

        codes: set[str] = set()
        for t in texts:
            codes.update(scan_injection(t))
        if codes:
            self._injection_findings.append((f"{kind}:{item_name}", sorted(codes)))
            logger.warning(
                "MCP server '{}': {} '{}' metadata flagged ({}). "
                "Registered anyway — inspect this server's {} description.",
                self.name, kind, item_name, ", ".join(sorted(codes)), kind,
            )

    def _enforce_spawn_policy(self, command: str, args: object) -> None:
        """Check spawn command for interpreter+egress shape; enforce per policy."""
        policy = getattr(self._cfg, "spawn_egress_policy", "warn")
        if policy == "off":
            return
        from durin.agent.tools.mcp_security import scan_spawn_command

        codes = scan_spawn_command(command, args)
        if not codes:
            return
        detail = ", ".join(codes)
        if policy == "refuse":
            raise PermissionError(
                f"MCP server '{self.name}': refusing to spawn — command matches "
                f"network-egress shape ({detail}). Set spawn_egress_policy='warn' "
                f"to allow, or fix the command/args."
            )
        logger.warning(
            "MCP server '{}': spawn command matches network-egress shape ({}). "
            "Spawning anyway (spawn_egress_policy='warn'). Verify this server entry "
            "came from a trusted source.",
            self.name, detail,
        )

    # ----- transport -----

    async def _open_transport_streams(self):
        """Open the transport and return (read_stream, write_stream).

        Lives in its own coroutine so the in-process test harness can
        monkeypatch it. Selects stdio / SSE / streamableHttp based on
        ``cfg.type`` (auto-detected if None). streamableHttp falls back
        to SSE on failure, with an auth-aware early exit.
        """
        cfg = self._cfg
        transport = cfg.type or self._infer_transport()
        if transport == "stdio":
            return await self._open_stdio()
        if transport == "sse":
            return await self._open_sse()
        if transport == "streamableHttp":
            try:
                return await self._open_streamable_http()
            except BaseException as exc:  # noqa: BLE001
                if _is_auth_error(exc):
                    # 401/OAuth error: SSE won't fix it — fail fast.
                    raise
                logger.warning(
                    "MCP server '{}': streamableHttp failed ({}), falling back to SSE",
                    self.name, type(exc).__name__,
                )
                await self._close_transport_streams()
                return await self._open_sse()
        raise ValueError(f"MCP server '{self.name}': unknown transport '{transport}'")

    def _infer_transport(self) -> str:
        cfg = self._cfg
        if cfg.command:
            return "stdio"
        if cfg.url:
            return "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
        raise ValueError(f"MCP server '{self.name}': no command or url configured")

    async def _open_stdio(self):
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        cfg = self._cfg
        self._enforce_spawn_policy(cfg.command, cfg.args)
        if getattr(cfg, "malware_check", True):
            from durin.agent.tools.mcp_security import check_package_for_malware

            finding = check_package_for_malware(cfg.command, cfg.args)
            if finding:
                raise PermissionError(
                    f"MCP server '{self.name}': {finding}"
                )
        command, args, env = _normalize_windows_stdio_command(
            cfg.command, cfg.args, _resolve_secret_map(cfg.env) or None
        )
        params = StdioServerParameters(command=command, args=args, env=env)
        errlog = _mcp_stderr_log()
        try:
            errlog.write(f"\n=== MCP server '{self.name}' stdio session ===\n")
            errlog.flush()
        except Exception:  # noqa: BLE001
            pass
        self._transport_cm = stdio_client(params, errlog=errlog)
        return await self._transport_cm.__aenter__()

    async def _open_streamable_http(self):
        from mcp.client.streamable_http import streamable_http_client

        cfg = self._cfg
        if not await _probe_http_url(cfg.url):
            raise ConnectionError(f"{cfg.url} unreachable")
        self._http_client = self._build_http_client(
            dict(
                headers=_resolve_secret_map(cfg.headers) or None,
                follow_redirects=True,
                max_redirects=_MCP_MAX_REDIRECTS,
                timeout=None,
                auth=self._oauth_provider,  # SP-4: None unless oauth configured
            )
        )
        await self._http_client.__aenter__()
        self._transport_cm = streamable_http_client(cfg.url, http_client=self._http_client)
        read, write, _ = await self._transport_cm.__aenter__()
        return read, write

    async def _open_sse(self):
        from mcp.client.sse import sse_client

        cfg = self._cfg
        if not await _probe_http_url(cfg.url):
            raise ConnectionError(f"{cfg.url} unreachable")
        self._transport_cm = sse_client(cfg.url, httpx_client_factory=self._sse_client_factory())
        return await self._transport_cm.__aenter__()

    async def _close_transport_streams(self) -> None:
        import contextlib
        cm = getattr(self, "_transport_cm", None)
        if cm is not None:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            self._transport_cm = None
        client = getattr(self, "_http_client", None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)
            self._http_client = None

    # ----- lifecycle -----

    async def start(self) -> bool:
        self._task = asyncio.ensure_future(self.run())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            # The serve loop never reached a terminal state (success or
            # failure) within the budget — it is stuck inside the transport
            # connect (see _CONNECT_TIMEOUT). Cancel the hung task and report a
            # normal per-server failure so connect_mcp_servers moves on and the
            # agent loop can start consuming messages.
            self._error = self._error or TimeoutError(
                f"connect timed out after {_CONNECT_TIMEOUT:.0f}s "
                f"(server may need interactive auth: durin mcp login {self.name})"
            )
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            return False
        return self._error is None and self.session is not None

    async def run(self) -> None:
        retries = 0
        initial_retries = 0
        backoff = _INITIAL_BACKOFF

        while True:
            try:
                await self._serve_once()
                # Clean return = shutdown or reconnect requested.
                if self._shutdown_event.is_set():
                    break
                logger.info("MCP server '{}': reconnect requested", self.name)
                retries = 0
                initial_retries = 0
                backoff = _INITIAL_BACKOFF
                self.session = None
                continue
            except asyncio.CancelledError:
                self.session = None
                raise
            except BaseException as exc:  # noqa: BLE001
                self.session = None
                if self._shutdown_event.is_set():
                    return
                if not self._ready.is_set():
                    # Initial connect failed.
                    if _is_auth_error(exc):
                        hint = (
                            f" Run: durin mcp login {self.name}"
                            if self._oauth_provider is not None
                            else ""
                        )
                        logger.warning(
                            "MCP server '{}': initial auth failed, not retrying: {}.{}",
                            self.name, exc, hint,
                        )
                        self._error = exc
                        self._ready.set()
                        return
                    initial_retries += 1
                    if initial_retries > _MAX_INITIAL_CONNECT_RETRIES:
                        logger.warning(
                            "MCP server '{}': initial connect gave up after {} tries: {}",
                            self.name, _MAX_INITIAL_CONNECT_RETRIES, exc,
                        )
                        self._error = exc
                        self._ready.set()
                        return
                    logger.warning(
                        "MCP server '{}': initial connect attempt {}/{}, retry in {:.0f}s: {}",
                        self.name, initial_retries, _MAX_INITIAL_CONNECT_RETRIES, backoff, exc,
                    )
                else:
                    # Post-connect drop.
                    retries += 1
                    if retries > _MAX_RECONNECT_RETRIES:
                        logger.warning(
                            "MCP server '{}': gave up after {} reconnect attempts: {}",
                            self.name, _MAX_RECONNECT_RETRIES, exc,
                        )
                        self._ready.set()
                        return
                    logger.warning(
                        "MCP server '{}': connection lost (attempt {}/{}), reconnect in {:.0f}s: {}",
                        self.name, retries, _MAX_RECONNECT_RETRIES, backoff, exc,
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                if self._shutdown_event.is_set():
                    self._ready.set()
                    return
            finally:
                self.session = None

    async def _serve_once(self) -> None:
        """One transport+session lifetime: connect, register, park, teardown.

        Enters and exits the transport + ClientSession context in THIS task.
        """
        from mcp import ClientSession

        try:
            read, write = await self._open_transport_streams()
            async with ClientSession(read, write, **self._session_kwargs()) as session:
                self.initialize_result = await session.initialize()
                self._refresh_generation += 1  # invalidate any in-flight refresh from prior session
                self.session = session
                await self._register_capabilities()
                self._reset_breaker()
                self._ready.set()
                await self._wait_for_lifecycle_event()
        finally:
            self.session = None
            await self._close_transport_streams()

    def _session_kwargs(self) -> dict:
        kwargs: dict = {
            "message_handler": self._make_message_handler(),
            "list_roots_callback": self._make_list_roots_callback(),
            "logging_callback": self._make_logging_callback(),
        }
        runner = self._sampling_runner
        if runner is not None:
            import mcp.types as types

            kwargs["sampling_callback"] = self._make_sampling_callback()
            if runner.governance.allow_tools:
                kwargs["sampling_capabilities"] = types.SamplingCapability(
                    tools=types.SamplingToolsCapability()
                )
        return kwargs

    def _make_list_roots_callback(self):
        async def _list_roots(context) -> Any:
            import mcp.types as types

            ws = self._workspace
            if not ws:
                return types.ListRootsResult(roots=[])
            from pathlib import Path
            uri = Path(ws).expanduser().resolve().as_uri()
            return types.ListRootsResult(
                roots=[types.Root(uri=uri, name="workspace")]
            )

        return _list_roots

    def _make_logging_callback(self):
        from durin.agent.tools.mcp_sampling import mcp_log_level_to_loguru

        async def _logging(params) -> None:
            level = mcp_log_level_to_loguru(getattr(params, "level", "info"))
            src = getattr(params, "logger", None) or "server"
            data = getattr(params, "data", "")
            logger.log(level, "MCP '{}' [{}]: {}", self.name, src, data)

        return _logging

    def _make_sampling_callback(self):
        async def _sampling(context, params) -> Any:
            runner = self._sampling_runner
            if runner is None:
                import mcp.types as types
                return types.ErrorData(
                    code=types.INVALID_REQUEST, message="sampling not enabled"
                )
            try:
                return await runner.run(params)
            except Exception as e:  # noqa: BLE001
                import mcp.types as types
                logger.exception("MCP '{}': sampling callback error", self.name)
                return types.ErrorData(
                    code=types.INTERNAL_ERROR, message=f"sampling failed: {e}"
                )

        return _sampling

    def _make_message_handler(self):
        async def _handler(message) -> None:
            try:
                if isinstance(message, Exception):
                    return
                # Lazy lookup so the fake-mcp tests (sys.modules["mcp"] is a stub
                # ModuleType with no real subpackage) don't ImportError at session
                # construction time.
                import sys as _sys
                _types = _sys.modules.get("mcp.types")
                if _types is None:
                    try:
                        import mcp.types as _types  # noqa: PLC0415
                    except ImportError:
                        return
                _server_notification_cls = getattr(_types, "ServerNotification", None)
                _list_changed_cls = getattr(_types, "ToolListChangedNotification", None)
                if (
                    _server_notification_cls is not None
                    and _list_changed_cls is not None
                    and isinstance(message, _server_notification_cls)
                    and isinstance(message.root, _list_changed_cls)
                ):
                    logger.info("MCP server '{}': tools/list_changed", self.name)
                    self._schedule_refresh()
                    await asyncio.sleep(0)  # let tests observe the scheduled task
            except Exception:  # noqa: BLE001
                logger.exception("MCP '{}': message handler error", self.name)

        return _handler

    def _schedule_refresh(self) -> asyncio.Task:
        task = asyncio.ensure_future(self._refresh_tools_task())
        self._pending_refresh.add(task)
        task.add_done_callback(self._pending_refresh.discard)
        return task

    async def _refresh_tools_task(self) -> None:
        try:
            await self._refresh_tools()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("MCP '{}': dynamic refresh failed", self.name)

    async def _refresh_tools(self) -> None:
        if not self._advertises_tools():
            return
        my_generation = self._refresh_generation
        my_session = self.session
        if my_session is None:
            return
        async with self._refresh_lock:
            # Generation guard: a newer refresh (or reconnect) superseded us.
            if my_generation != self._refresh_generation:
                logger.debug("MCP '{}': stale refresh (gen) — skipping", self.name)
                return
            # Identity guard: session was replaced since we were scheduled.
            if my_session is not self.session:
                logger.debug("MCP '{}': stale refresh (identity) — skipping", self.name)
                return
            old = set(self._registered_names)
            async with self._rpc_lock:
                tools_result = await my_session.list_tools()
            # Re-check after the await: the world may have moved.
            if my_generation != self._refresh_generation or my_session is not self.session:
                logger.debug("MCP '{}': refresh invalidated mid-flight — skipping", self.name)
                return
            new_mcp = tools_result.tools if hasattr(tools_result, "tools") else []
            new_wrapped = {
                _sanitize_name(f"mcp_{self.name}_{t.name}") for t in new_mcp
            }
            # In-place: deregister only names that vanished.
            for stale in old - new_wrapped:
                self._registry.unregister(stale)
            await self._register_capabilities()
            added = set(self._registered_names) - old
            removed = old - set(self._registered_names)
            if added or removed:
                logger.warning(
                    "MCP server '{}': tools changed — added {}, removed {}",
                    self.name, sorted(added), sorted(removed),
                )

    async def _wait_for_lifecycle_event(self) -> None:
        """Park until shutdown or reconnect; heartbeat the session between.

        Returns when _shutdown_event or _reconnect_event fires. On a failed
        heartbeat, sets _reconnect_event and returns.
        """
        shutdown_task = asyncio.ensure_future(self._shutdown_event.wait())
        reconnect_task = asyncio.ensure_future(self._reconnect_event.wait())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {shutdown_task, reconnect_task},
                    timeout=self._keepalive_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    break
                if self.session is not None:
                    try:
                        if self._advertises_tools():
                            await asyncio.wait_for(
                                self.session.list_tools(), timeout=_KEEPALIVE_TIMEOUT
                            )
                        else:
                            await asyncio.wait_for(
                                self.session.send_ping(), timeout=_KEEPALIVE_TIMEOUT
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "MCP server '{}': keepalive failed, reconnecting: {}",
                            self.name, exc,
                        )
                        self._reconnect_event.set()
                        break
        finally:
            for t in (shutdown_task, reconnect_task):
                if not t.done():
                    t.cancel()
                    with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
                        await t
        if self._reconnect_event.is_set() and not self._shutdown_event.is_set():
            self._reconnect_event.clear()

    async def aclose(self) -> None:
        self._shutdown_event.set()
        self._reconnect_event.set()
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        for task in list(self._pending_refresh):
            task.cancel()
        if self._pending_refresh:
            await asyncio.gather(*self._pending_refresh, return_exceptions=True)
            self._pending_refresh.clear()
        for tool_name in list(self._registered_names):
            self._registry.unregister(tool_name)
        self._registered_names = []
        self.session = None

    # ----- capability registration -----

    def _advertises_tools(self) -> bool:
        init = self.initialize_result
        caps = getattr(init, "capabilities", None) if init is not None else None
        if caps is None:
            return True  # legacy fallback: preserve always-list behavior
        return getattr(caps, "tools", None) is not None

    async def _register_capabilities(self) -> None:
        """Discover + register tools/resources/prompts for the live session.

        Reused by initial connect, reconnect, and list_changed refresh.
        Wrappers are constructed with ``self`` (the connection), not the
        raw session, so they re-resolve the live session per call.
        """
        session = self.session
        if session is None:
            return
        cfg = self._cfg
        new_names: list[str] = []

        if self._advertises_tools():
            catalog_timeout = float(getattr(cfg, "catalog_timeout", 1.5))
            tools = await asyncio.wait_for(session.list_tools(), timeout=catalog_timeout)
            _disable_output_schema_validation(session)
            enabled = set(cfg.enabled_tools)
            allow_all = "*" in enabled
            available_raw = [t.name for t in tools.tools]
            available_wrapped = [_sanitize_name(f"mcp_{self.name}_{t.name}") for t in tools.tools]
            matched: set[str] = set()
            for tool_def in tools.tools:
                wrapped = _sanitize_name(f"mcp_{self.name}_{tool_def.name}")
                if not allow_all and tool_def.name not in enabled and wrapped not in enabled:
                    continue
                self._scan_metadata("tool", tool_def.name, tool_def.description)
                self._registry.register(
                    MCPToolWrapper(self, self.name, tool_def, tool_timeout=cfg.tool_timeout)
                )
                new_names.append(wrapped)
                if enabled:
                    if tool_def.name in enabled:
                        matched.add(tool_def.name)
                    if wrapped in enabled:
                        matched.add(wrapped)
            if enabled and not allow_all:
                unmatched = sorted(enabled - matched)
                if unmatched:
                    logger.warning(
                        "MCP server '{}': enabledTools entries not found: {}. "
                        "Available raw names: {}. Available wrapped names: {}",
                        self.name,
                        ", ".join(unmatched),
                        ", ".join(available_raw) or "(none)",
                        ", ".join(available_wrapped) or "(none)",
                    )
        else:
            logger.info("MCP server '{}': no tools capability; skipping tools/list", self.name)

        try:
            resources = await session.list_resources()
            for resource in resources.resources:
                self._scan_metadata("resource", str(resource.uri), resource.description, str(resource.uri))
                w = MCPResourceWrapper(self, self.name, resource, resource_timeout=cfg.tool_timeout)
                self._registry.register(w)
                new_names.append(w.name)
        except Exception as e:  # noqa: BLE001
            logger.debug("MCP '{}': resources unsupported: {}", self.name, e)

        try:
            prompts = await session.list_prompts()
            for prompt in prompts.prompts:
                self._scan_metadata("prompt", prompt.name, prompt.description)
                w = MCPPromptWrapper(self, self.name, prompt, prompt_timeout=cfg.tool_timeout)
                self._registry.register(w)
                new_names.append(w.name)
        except Exception as e:  # noqa: BLE001
            logger.debug("MCP '{}': prompts unsupported: {}", self.name, e)

        self._registered_names = new_names
        if self._defer_cb is not None:
            try:
                self._defer_cb()
            except Exception as e:  # noqa: BLE001
                logger.warning("MCP '{}': deferral re-apply failed: {}", self.name, e)
        logger.info("MCP server '{}': registered {} capabilities", self.name, len(new_names))

    # ----- call surface (live-session indirection) -----

    def _resolve_session(self) -> Any | None:
        return self.session

    async def call_tool(self, original_name: str, arguments: dict, timeout: float) -> Any:
        down = self._breaker_precheck()
        if down is not None:
            return down
        session = self._resolve_session()
        if session is None:
            self._bump_error()
            return _ConnDown(f"MCP server '{self.name}' is not connected")
        try:
            result = await self._raw_call_tool(session, original_name, arguments, timeout)
            self._reset_breaker()
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            if _is_auth_error(exc) and self._oauth_provider is not None:
                # SP-4d: OAuth 401 the SDK couldn't silently refresh → ask the
                # user to re-auth. NOT a transient drop, so no reconnect/retry.
                self._bump_error()
                logger.warning("MCP '{}': OAuth 401 — re-auth required", self.name)
                return _ConnDown(
                    f"MCP server '{self.name}' needs OAuth re-authentication. "
                    f"Run: durin mcp login {self.name} — do NOT retry this tool "
                    f"until sign-in completes."
                )
            if _is_timeout_error(exc):
                self._bump_error()
                return _ConnDown(
                    f"(MCP tool '{original_name}' on '{self.name}' timed out after {timeout}s)"
                )
            if _is_session_expired_error(exc) or _is_transient_conn(exc):
                recovered = await self._recover_and_retry_tool(session, original_name, arguments, timeout)
                if recovered is not None:
                    return recovered
            self._bump_error()
            logger.warning("MCP '{}' tool '{}' failed: {}", self.name, original_name, exc)
            return _ConnDown(f"MCP server '{self.name}' tool call failed: {type(exc).__name__}")

    async def _raw_call_tool(self, session, original_name, arguments, timeout):
        """Call with an IDLE timeout that resets on progress (opencode behavior).

        ``timeout`` is the max idle gap (seconds) with no progress. A tool that
        keeps reporting progress stays alive; a tool that goes silent for
        ``timeout`` seconds is cancelled and raises asyncio.TimeoutError.
        """
        loop = asyncio.get_event_loop()
        last_progress = loop.time()

        async def _progress(progress: float, total: float | None = None, message: str | None = None) -> None:
            nonlocal last_progress
            last_progress = loop.time()

        async with self._rpc_lock:
            call_task = asyncio.ensure_future(
                session.call_tool(original_name, arguments=arguments, progress_callback=_progress)
            )
            try:
                while True:
                    remaining = timeout - (loop.time() - last_progress)
                    if remaining <= 0:
                        call_task.cancel()
                        with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
                            await call_task
                        raise asyncio.TimeoutError(f"no progress for {timeout}s")
                    done, _ = await asyncio.wait({call_task}, timeout=remaining)
                    if call_task in done:
                        return call_task.result()
                    # No completion within remaining window — check if progress updated
                    # (loop back to recalculate remaining from last_progress).
            finally:
                if not call_task.done():
                    call_task.cancel()
                    with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
                        await call_task

    async def _recover_and_retry_tool(self, stale: Any, original_name, arguments, timeout):
        """Request a transport reconnect, wait briefly for a fresh session,
        retry once. Returns the retry result, a state-lost sentinel, or None
        (caller falls through to the generic error)."""
        self._request_reconnect()
        fresh = await self._await_fresh_session(stale, timeout=15.0)
        if fresh is None:
            return _ConnDown(
                f"(MCP server '{self.name}' restarted; session state lost)"
            )
        try:
            result = await self._raw_call_tool(fresh, original_name, arguments, timeout)
            self._reset_breaker()
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            logger.warning("MCP '{}' retry after reconnect failed: {}", self.name, exc)
            return None

    async def _await_fresh_session(self, stale: Any, timeout: float) -> Any | None:
        """Wait for a reconnect to install a session distinct from ``stale``.

        Identity-based (not None-transition based): a fast reconnect's
        ``session is None`` window can be sub-millisecond and invisible to the
        poll, so we wait for ``self.session`` to become a NEW non-None object
        instead. Bounded by ``timeout`` (returns None if reconnect never lands).
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            current = self.session
            if current is not None and current is not stale:
                return current
            await asyncio.sleep(0.05)
        return None

    def _request_reconnect(self) -> None:
        self._reconnect_event.set()

    async def read_resource(self, uri: Any, timeout: float) -> Any:
        down = self._breaker_precheck()
        if down is not None:
            return down
        session = self._resolve_session()
        if session is None:
            self._bump_error()
            return _ConnDown(f"MCP server '{self.name}' is not connected")
        try:
            async with self._rpc_lock:
                result = await asyncio.wait_for(session.read_resource(uri), timeout=timeout)
            self._reset_breaker()
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            if _is_timeout_error(exc):
                self._bump_error()
                return _ConnDown(f"(MCP resource read on '{self.name}' timed out after {timeout}s)")
            if _is_session_expired_error(exc) or _is_transient_conn(exc):
                self._request_reconnect()
                fresh = await self._await_fresh_session(session, timeout=15.0)
                if fresh is None:
                    return _ConnDown(
                        f"(MCP server '{self.name}' restarted; session state lost)"
                    )
                try:
                    async with self._rpc_lock:
                        result = await asyncio.wait_for(fresh.read_resource(uri), timeout=timeout)
                    self._reset_breaker()
                    return result
                except asyncio.CancelledError:
                    raise
                except BaseException:  # noqa: BLE001
                    pass
            self._bump_error()
            logger.warning("MCP '{}' resource read failed: {}", self.name, exc)
            return _ConnDown(f"MCP server '{self.name}' resource read failed: {type(exc).__name__}")

    async def get_prompt(self, name: str, arguments: dict, timeout: float) -> Any:
        down = self._breaker_precheck()
        if down is not None:
            return down
        session = self._resolve_session()
        if session is None:
            self._bump_error()
            return _ConnDown(f"MCP server '{self.name}' is not connected")
        try:
            async with self._rpc_lock:
                result = await asyncio.wait_for(
                    session.get_prompt(name, arguments=arguments), timeout=timeout
                )
            self._reset_breaker()
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            if _is_timeout_error(exc):
                self._bump_error()
                return _ConnDown(f"(MCP prompt '{name}' on '{self.name}' timed out after {timeout}s)")
            if _is_session_expired_error(exc) or _is_transient_conn(exc):
                self._request_reconnect()
                fresh = await self._await_fresh_session(session, timeout=15.0)
                if fresh is None:
                    return _ConnDown(
                        f"(MCP server '{self.name}' restarted; session state lost)"
                    )
                try:
                    async with self._rpc_lock:
                        result = await asyncio.wait_for(
                            fresh.get_prompt(name, arguments=arguments), timeout=timeout
                        )
                    self._reset_breaker()
                    return result
                except asyncio.CancelledError:
                    raise
                except BaseException:  # noqa: BLE001
                    pass
            self._bump_error()
            logger.warning("MCP '{}' prompt '{}' failed: {}", self.name, name, exc)
            return _ConnDown(f"MCP server '{self.name}' prompt call failed: {type(exc).__name__}")

    # ----- circuit breaker -----

    def breaker_state(self) -> BreakerState:
        if self._error_count < _CIRCUIT_BREAKER_THRESHOLD:
            return BreakerState.CLOSED
        opened = self._breaker_opened_at or 0.0
        if (asyncio.get_event_loop().time() - opened) < _CIRCUIT_BREAKER_COOLDOWN_SEC:
            return BreakerState.OPEN
        return BreakerState.HALF_OPEN

    def _breaker_precheck(self) -> Any | None:
        if self.breaker_state() is BreakerState.OPEN:
            opened = self._breaker_opened_at or 0.0
            remaining = max(
                1, int(_CIRCUIT_BREAKER_COOLDOWN_SEC - (asyncio.get_event_loop().time() - opened))
            )
            return _ConnDown(
                f"MCP server '{self.name}' is unreachable after {self._error_count} "
                f"consecutive failures. Auto-retry available in ~{remaining}s. "
                f"Do NOT retry this tool yet — use alternative approaches or ask "
                f"the user to check the MCP server."
            )
        return None  # CLOSED or HALF_OPEN (let the call through as a probe)

    def _bump_error(self) -> None:
        self._error_count += 1
        if self._error_count >= _CIRCUIT_BREAKER_THRESHOLD:
            self._breaker_opened_at = asyncio.get_event_loop().time()

    def _reset_breaker(self) -> None:
        self._error_count = 0
        self._breaker_opened_at = None
