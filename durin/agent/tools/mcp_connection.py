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
import datetime as _dt
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from loguru import logger

from durin.agent.tools.registry import ToolRegistry

# Reuse SP-1 transport helpers + render-free building blocks from mcp.py.
from durin.agent.tools.mcp import (
    _normalize_windows_stdio_command,
    _probe_http_url,
    _sanitize_name,
    _disable_output_schema_validation,
    MCPToolWrapper,
    MCPResourceWrapper,
    MCPPromptWrapper,
)


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


# Reconnect / backoff constants (monkeypatchable in tests).
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
_MAX_INITIAL_CONNECT_RETRIES = 3
_MAX_RECONNECT_RETRIES = 5
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
    ) -> None:
        self.name = name
        self._cfg = cfg
        self._registry = registry
        self._defer_cb = defer_cb

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

    # ----- transport -----

    async def _open_transport_streams(self):
        """Open the transport and return (read_stream, write_stream).

        Lives in its own coroutine so the in-process test harness can
        monkeypatch it. The async-context objects it enters are owned by
        run()'s ``async with`` (entered + exited in the task).
        """
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        cfg = self._cfg
        command, args, env = _normalize_windows_stdio_command(
            cfg.command, cfg.args, cfg.env or None
        )
        params = StdioServerParameters(command=command, args=args, env=env)
        self._transport_cm = stdio_client(params)
        read, write = await self._transport_cm.__aenter__()
        return read, write

    async def _close_transport_streams(self) -> None:
        cm = getattr(self, "_transport_cm", None)
        if cm is not None:
            await cm.__aexit__(None, None, None)
            self._transport_cm = None

    # ----- lifecycle -----

    async def start(self) -> bool:
        self._task = asyncio.ensure_future(self.run())
        await self._ready.wait()
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
                        logger.warning(
                            "MCP server '{}': initial auth failed, not retrying: {}",
                            self.name, exc,
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
                self.session = session
                await self._register_capabilities()
                self._reset_breaker()
                self._ready.set()
                await self._wait_for_lifecycle_event()
        finally:
            self.session = None
            await self._close_transport_streams()

    def _session_kwargs(self) -> dict:
        """ClientSession kwargs. 2d adds message_handler here."""
        return {}

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
            tools = await session.list_tools()
            _disable_output_schema_validation(session)
            enabled = set(cfg.enabled_tools)
            allow_all = "*" in enabled
            for tool_def in tools.tools:
                wrapped = _sanitize_name(f"mcp_{self.name}_{tool_def.name}")
                if not allow_all and tool_def.name not in enabled and wrapped not in enabled:
                    continue
                self._registry.register(
                    MCPToolWrapper(self, self.name, tool_def, tool_timeout=cfg.tool_timeout)
                )
                new_names.append(wrapped)
        else:
            logger.info("MCP server '{}': no tools capability; skipping tools/list", self.name)

        try:
            resources = await session.list_resources()
            for resource in resources.resources:
                w = MCPResourceWrapper(self, self.name, resource, resource_timeout=cfg.tool_timeout)
                self._registry.register(w)
                new_names.append(w.name)
        except Exception as e:  # noqa: BLE001
            logger.debug("MCP '{}': resources unsupported: {}", self.name, e)

        try:
            prompts = await session.list_prompts()
            for prompt in prompts.prompts:
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
            if _is_session_expired_error(exc) or _is_transient_conn(exc):
                recovered = await self._recover_and_retry_tool(original_name, arguments, timeout)
                if recovered is not None:
                    return recovered
            self._bump_error()
            logger.warning("MCP '{}' tool '{}' failed: {}", self.name, original_name, exc)
            return _ConnDown(f"MCP server '{self.name}' tool call failed: {type(exc).__name__}")

    async def _raw_call_tool(self, session, original_name, arguments, timeout):
        async with self._rpc_lock:
            return await session.call_tool(
                original_name, arguments=arguments,
                read_timeout_seconds=_dt.timedelta(seconds=timeout),
            )

    async def _recover_and_retry_tool(self, original_name, arguments, timeout):
        """Request a transport reconnect, wait briefly for a fresh session,
        retry once. Returns the retry result, a state-lost sentinel, or None
        (caller falls through to the generic error)."""
        self._request_reconnect()
        fresh = await self._await_fresh_session(timeout=15.0)
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

    async def _await_fresh_session(self, timeout: float) -> Any | None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self.session is not None:
                return self.session
            await asyncio.sleep(0.1)
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
                result = await session.read_resource(uri)
            self._reset_breaker()
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            if _is_session_expired_error(exc) or _is_transient_conn(exc):
                self._request_reconnect()
                fresh = await self._await_fresh_session(timeout=15.0)
                if fresh is None:
                    return _ConnDown(
                        f"(MCP server '{self.name}' restarted; session state lost)"
                    )
                try:
                    async with self._rpc_lock:
                        result = await fresh.read_resource(uri)
                    self._reset_breaker()
                    return result
                except asyncio.CancelledError:
                    raise
                except BaseException:  # noqa: BLE001
                    pass
            self._bump_error()
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
                result = await session.get_prompt(name, arguments=arguments)
            self._reset_breaker()
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            if _is_session_expired_error(exc) or _is_transient_conn(exc):
                self._request_reconnect()
                fresh = await self._await_fresh_session(timeout=15.0)
                if fresh is None:
                    return _ConnDown(
                        f"(MCP server '{self.name}' restarted; session state lost)"
                    )
                try:
                    async with self._rpc_lock:
                        result = await fresh.get_prompt(name, arguments=arguments)
                    self._reset_breaker()
                    return result
                except asyncio.CancelledError:
                    raise
                except BaseException:  # noqa: BLE001
                    pass
            self._bump_error()
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
