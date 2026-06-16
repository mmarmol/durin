"""Starlette ASGI app — the HTTP front door (SP4 reads + SP5 writes).

Exports:
    resolve_principal_from_headers  — extract + verify a bearer token.
    build_api_app                   — build the Starlette app (reads + writes).
    build_gateway_http_app          — full gateway HTTP surface (step 2 of ASGI unify).

The app mounts every ``@route``-decorated service method from the registry:
- GET + ``:read`` scope  → read handler (query/path params → request model).
- POST/DELETE/PATCH      → write handler (JSON body merged with path params).

DomainError codes are mapped to RFC-9457 problem+json responses.

The controller (durin/cli/commands.py) is responsible for:
    - building the ServiceRegistry with real deps,
    - calling build_api_app(),
    - running uvicorn.Server(Config(app, ...)).serve() inside the loop.
"""

from __future__ import annotations

import hmac
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from durin.service.auth import AuthService
from durin.service.principal import Principal, Scope
from durin.service.registry import BoundRoute, ServiceRegistry
from durin.service.types import (
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnauthenticatedError,
    UnavailableError,
    ValidationFailedError,
)

if TYPE_CHECKING:
    from durin.channels.websocket import WebSocketChannel

# ---------------------------------------------------------------------------
# Starlette WebSocket connection adapter
# ---------------------------------------------------------------------------


class StarletteConnectionAdapter:
    """ConnectionAdapter backed by a Starlette WebSocket.

    Satisfies the same interface as ``durin.channels.websocket.ConnectionAdapter``
    so that ``WebSocketChannel._run_connection`` works unchanged with both the
    websockets and the Starlette transports.

    Iteration yields inbound text frames; ``WebSocketDisconnect`` stops the
    iteration cleanly.  The instance itself is the identity key in ``_subs``
    (default object identity is hashable and stable per connection).
    """

    __slots__ = ("_ws", "_iter")

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._iter: Any = None

    async def send_text(self, raw: str) -> None:
        await self._ws.send_text(raw)

    @property
    def remote(self) -> Any:
        return self._ws.client

    def __aiter__(self) -> "StarletteConnectionAdapter":
        self._iter = self._ws.iter_text().__aiter__()
        return self

    async def __anext__(self) -> str:
        if self._iter is None:
            self._iter = self._ws.iter_text().__aiter__()
        try:
            return await self._iter.__anext__()
        except (WebSocketDisconnect, StopAsyncIteration):
            raise StopAsyncIteration

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._ws.close(code)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# DomainError → HTTP status map
# ---------------------------------------------------------------------------

_ERROR_STATUS: dict[str, int] = {
    UnauthenticatedError.code: 401,
    ForbiddenError.code: 403,
    NotFoundError.code: 404,
    ConflictError.code: 409,
    ValidationFailedError.code: 422,
    TooManyRequestsError.code: 429,
    UnavailableError.code: 503,
}

_ERROR_TITLE: dict[str, str] = {
    UnauthenticatedError.code: "Unauthenticated",
    ForbiddenError.code: "Forbidden",
    NotFoundError.code: "Not Found",
    ConflictError.code: "Conflict",
    ValidationFailedError.code: "Unprocessable Entity",
    TooManyRequestsError.code: "Too Many Requests",
    UnavailableError.code: "Service Unavailable",
}


def _problem_response(err: DomainError) -> Response:
    """Map a DomainError to an RFC-9457 application/problem+json response.

    ``details`` (when present) is echoed as an extension member so a domain
    payload — e.g. the skills approval gate's ``{refused, verdict, message}`` or
    forget's ``{result}`` — reaches the client inside the one error format.
    """
    status = _ERROR_STATUS.get(err.code, 500)
    title = _ERROR_TITLE.get(err.code, "Internal Server Error")
    problem: dict[str, Any] = {
        "type": f"urn:durin:error:{err.code}",
        "title": title,
        "status": status,
        "detail": err.message,
    }
    if err.details:
        problem["details"] = err.details
    return Response(
        json.dumps(problem), status_code=status, media_type="application/problem+json"
    )


# ---------------------------------------------------------------------------
# Principal resolver
# ---------------------------------------------------------------------------


def resolve_principal_from_headers(
    headers: Any,
    *,
    auth: AuthService,
    static_token: str = "",
) -> Principal | None:
    """Extract and verify a bearer token from request headers.

    Priority:
      1. ``Authorization: Bearer <token>`` → ``auth.resolve(token)`` (persisted store).
      2. If that returns None AND the token equals ``static_token`` (non-empty)
         → ``Principal.remote("static", {Scope.ADMIN})``.
      3. Otherwise → ``None`` (caller should return 401).

    The legacy in-memory dual-accept lives in the WS channel only; this
    function is the clean surface for the new door.
    """
    auth_header: str = headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[7:]
    principal = auth.resolve(token)
    if principal is not None:
        return principal
    if static_token and hmac.compare_digest(token, static_token):
        return Principal.remote("static", frozenset({Scope.ADMIN.value}))
    return None


# ---------------------------------------------------------------------------
# Generic read-route adapter
# ---------------------------------------------------------------------------


def _is_read_route(bound: BoundRoute) -> bool:
    """True for GET routes whose scope ends in ``:read`` (or have no scope)."""
    if bound.spec.verb != "GET":
        return False
    scope = bound.spec.scope
    return scope is None or scope.endswith(":read")


def _is_write_route(bound: BoundRoute) -> bool:
    """True for non-GET routes (POST, DELETE, PATCH, …)."""
    return bound.spec.verb != "GET"


def _build_422(exc: ValidationError) -> Response:
    body = json.dumps(
        {
            "type": "urn:durin:error:validation_failed",
            "title": "Unprocessable Entity",
            "status": 422,
            "detail": exc.errors(),
        }
    )
    return Response(body, status_code=422, media_type="application/problem+json")


def _result_response(result: Any) -> JSONResponse:
    """Serialize a service ``Result`` to a 200 JSON response.

    A returned ``Result`` is always a success: every non-2xx outcome is raised as
    a ``DomainError`` and rendered as problem+json by :func:`_problem_response`,
    so the API has ONE error format for every 4xx/5xx.
    """
    return JSONResponse(result.model_dump())


def _build_handler(
    bound: BoundRoute,
    *,
    auth: AuthService,
    static_token: str,
) -> Callable:
    """Return an async Starlette handler for a read (GET) BoundRoute.

    Path params and query params (including multi-value lists via .getlist)
    are merged by field name and passed to the request_model constructor.
    The handler:
      1. Resolves the principal (401 if missing).
      2. Builds the request model from params (422 on ValidationError).
      3. Awaits the service handler (DomainError → problem+json).
      4. Returns JSONResponse(result.model_dump()).
    """
    request_model = bound.spec.request_model
    handler = bound.handler

    async def endpoint(request: Request) -> Response:
        principal = resolve_principal_from_headers(
            request.headers, auth=auth, static_token=static_token
        )
        if principal is None:
            return _problem_response(
                UnauthenticatedError("Missing or invalid bearer token")
            )

        # Build input params: path params override query params of the same name.
        params: dict[str, Any] = {}
        if request_model is not None:
            for key, values in request.query_params.multi_items():
                existing = params.get(key)
                if existing is None:
                    params[key] = values
                elif isinstance(existing, list):
                    existing.append(values)
                else:
                    params[key] = [existing, values]
            # Path params always win (they come from the URL template).
            params.update(dict(request.path_params))

            try:
                model = request_model(**params)
            except ValidationError as exc:
                return _build_422(exc)
        else:
            model = None

        try:
            if model is not None:
                result = await handler(model, principal)
            else:
                result = await handler(principal)
        except DomainError as exc:
            return _problem_response(exc)

        return _result_response(result)

    return endpoint


def _build_write_handler(
    bound: BoundRoute,
    *,
    auth: AuthService,
    static_token: str,
) -> Callable:
    """Return an async Starlette handler for a write (POST/DELETE/PATCH) BoundRoute.

    The Command is built from ``await request.json()`` merged with path params.
    Path params win for any key present in both (e.g. ``{key}``, ``{name}``).
    An absent or empty body is treated as ``{}`` (allows DELETE with path-only params).
    The handler:
      1. Resolves the principal (401 if missing).
      2. Parses the JSON body (empty/absent → {}) merged with path params.
      3. Builds the request model (422 on ValidationError).
      4. Awaits the service handler (DomainError → problem+json).
      5. Returns JSONResponse(result.model_dump()).
    """
    request_model = bound.spec.request_model
    handler = bound.handler

    async def endpoint(request: Request) -> Response:
        principal = resolve_principal_from_headers(
            request.headers, auth=auth, static_token=static_token
        )
        if principal is None:
            return _problem_response(
                UnauthenticatedError("Missing or invalid bearer token")
            )

        params: dict[str, Any] = {}
        if request_model is not None:
            # Parse JSON body; treat missing/empty body as {}.
            body_bytes = await request.body()
            if body_bytes:
                try:
                    body_data = json.loads(body_bytes)
                    if isinstance(body_data, dict):
                        params.update(body_data)
                except (json.JSONDecodeError, ValueError):
                    pass
            # Path params always win over body fields.
            params.update(dict(request.path_params))

            try:
                model = request_model(**params)
            except ValidationError as exc:
                return _build_422(exc)
        else:
            model = None

        try:
            if model is not None:
                result = await handler(model, principal)
            else:
                result = await handler(principal)
        except DomainError as exc:
            return _problem_response(exc)

        return _result_response(result)

    return endpoint


# ---------------------------------------------------------------------------
# Request-ID middleware
# ---------------------------------------------------------------------------


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Generate/propagate ``X-Request-Id`` on every request."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_api_app(
    registry: ServiceRegistry,
    *,
    auth: AuthService,
    static_token: str = "",
) -> Starlette:
    """Build the Starlette ASGI app (reads + writes).

    Mounts:
    - ``GET /api/v1/health``         — unauthenticated liveness probe.
    - One Starlette Route per GET ``@route`` with ``:read`` scope (read handler).
    - One Starlette Route per non-GET ``@route`` (write handler, JSON body).

    Multiple routes on the same path (e.g. GET + POST /api/v1/settings) are
    collapsed into a single Starlette Route with both verbs listed; Starlette
    dispatches by method.

    Args:
        registry:     ServiceRegistry with all services registered.
        auth:         AuthService used to resolve bearer tokens.
        static_token: If non-empty, this plaintext token grants ADMIN access
                      (the bootstrap/static token from gateway config).
    """
    routes: list[Route] = [
        Route("/api/v1/health", _health_handler, methods=["GET"]),
    ]

    # Order routes so a literal segment is matched before a ``{param}`` at the
    # same position. Routes are collected in alphabetical method-name order, so
    # without this GET ``/skills/{name}`` shadows the literal ``/skills/quarantine``,
    # ``/skills/resolve``, ``/skills/search`` (Starlette matches in list order,
    # first match wins). Mapping a ``{param}`` segment to a high sentinel sorts
    # it after every literal — the universally-correct "most specific first".
    def _route_order(bound: BoundRoute) -> list[str]:
        return [
            "￿" if seg.startswith("{") else seg
            for seg in bound.spec.path.split("/")
        ]

    for bound in sorted(registry.routes, key=_route_order):
        if _is_read_route(bound):
            h = _build_handler(bound, auth=auth, static_token=static_token)
            routes.append(Route(bound.spec.path, h, methods=["GET"]))
        elif _is_write_route(bound):
            h = _build_write_handler(bound, auth=auth, static_token=static_token)
            routes.append(Route(bound.spec.path, h, methods=[bound.spec.verb]))

    return Starlette(
        routes=routes,
        middleware=[Middleware(RequestIdMiddleware)],
    )


async def _health_handler(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Gateway HTTP app factory
# ---------------------------------------------------------------------------


def build_gateway_http_app(
    channel: "WebSocketChannel",
    registry: ServiceRegistry,
    *,
    auth: AuthService,
    static_token: str = "",
    static_dist_path: Path | None = None,
) -> Starlette:
    """Build a Starlette ASGI app serving the full gateway HTTP surface.

    Mounts (in priority order; the literal/signed routes precede the generic
    /api/v1 table so the overrides win on first match):
    - WebSocket chat at the configured ws path — ConnectionAdapter-backed.
    - ``GET /api/v1/sessions/{key}/messages`` and ``/webui-thread`` — signed
      overrides that call the service, then sign media URLs via the channel.
    - ``/api/v1/*``                    — service front door (build_api_app routes).
    - ``GET /webui/bootstrap``         — token mint + session metadata.
    - ``GET /api/media/{sig}/{payload}`` — signed media fetch.
    - Static SPA files at ``/``        — served from ``static_dist_path`` if provided,
                                         with SPA history-mode fallback to index.html.

    The bootstrap and media handlers call the channel's ``bootstrap`` /
    ``media_fetch`` methods (which return plain data and raise ``DomainError`` on
    failure) and build the Starlette response — problem+json on a DomainError.

    Args:
        channel:          The live WebSocketChannel (owns business logic + token store).
        registry:         ServiceRegistry for ``/api/v1/*`` routes.
        auth:             AuthService for ``/api/v1/*`` bearer resolution.
        static_token:     Static bearer token for ``/api/v1/*``.
        static_dist_path: If provided, serve the built SPA from this directory.
                          Falls back to ``channel._static_dist_path`` if None.
    """
    resolved_static = static_dist_path or channel._static_dist_path

    # -- /webui/bootstrap ---------------------------------------------------

    async def bootstrap_handler(request: Request) -> Response:
        # Pass the REAL peer (request.client) so the channel's localhost gate
        # sees the actual client IP — a hardcoded localhost would let a remote
        # caller mint ADMIN tokens on a non-loopback bind. request.client is None
        # only when the ASGI server omits the peer; the gate then fails closed.
        try:
            payload = channel.bootstrap(peer=request.client, headers=request.headers)
        except DomainError as exc:
            return _problem_response(exc)
        return JSONResponse(payload)

    # -- /api/media/{sig}/{payload} -----------------------------------------

    async def media_handler(request: Request) -> Response:
        try:
            body, media_type, headers = channel.media_fetch(
                request.path_params["sig"], request.path_params["payload"]
            )
        except DomainError as exc:
            return _problem_response(exc)
        return Response(body, media_type=media_type, headers=dict(headers))

    # -- signed v1 session reads --------------------------------------------
    # Media-URL signing (and the webui-thread build) needs this channel's
    # per-process ``_media_secret`` — an adapter concern the generic /api/v1
    # handler cannot perform. These two routes call the service then sign, and
    # are registered ahead of the generic ``/api/v1/sessions/*`` routes so they
    # win (Starlette first-match).

    async def v1_session_messages(request: Request) -> Response:
        from durin.service.sessions import SessionMessagesQuery

        principal = resolve_principal_from_headers(
            request.headers, auth=auth, static_token=static_token
        )
        if principal is None:
            return _problem_response(
                UnauthenticatedError("Missing or invalid bearer token")
            )
        try:
            result = await registry.get("sessions").messages(
                SessionMessagesQuery(key=request.path_params["key"]), principal
            )
        except DomainError as exc:
            return _problem_response(exc)
        # Replace raw on-disk media paths with signed fetch URLs (in place).
        channel._augment_media_urls(result.data)
        return JSONResponse(result.model_dump())

    async def v1_webui_thread(request: Request) -> Response:
        from durin.service.sessions import WebuiThreadQuery
        from durin.utils.webui_transcript import build_webui_thread_response

        principal = resolve_principal_from_headers(
            request.headers, auth=auth, static_token=static_token
        )
        if principal is None:
            return _problem_response(
                UnauthenticatedError("Missing or invalid bearer token")
            )
        key = request.path_params["key"]
        try:
            # Validates the key + enforces scope; the real payload is built below
            # with the channel's signing callback (which the service cannot hold).
            await registry.get("sessions").webui_thread(
                WebuiThreadQuery(key=key), principal
            )
        except DomainError as exc:
            return _problem_response(exc)
        data = build_webui_thread_response(
            key, augment_user_media=channel._augment_transcript_user_media
        )
        if data is None:
            return _problem_response(
                NotFoundError("webui thread not found", details={"key": key})
            )
        return JSONResponse({"data": data})

    # -- WebSocket chat endpoint --------------------------------------------

    ws_path = channel._expected_path() or "/"

    async def chat_ws_endpoint(websocket: WebSocket) -> None:
        # Auth before accept — on reject close with 1008 (policy violation).
        query: dict[str, list[str]] = {}
        raw_query = websocket.query_params
        for k in raw_query.keys():
            query[k] = raw_query.getlist(k)

        if not channel._ws_auth_ok(query):
            await websocket.close(1008)
            return

        client_id_raw = websocket.query_params.get("client_id", "")
        client_id = client_id_raw.strip()[:128] if client_id_raw else ""
        if not channel.is_allowed(client_id):
            await websocket.close(1008)
            return

        await websocket.accept()

        adapter = StarletteConnectionAdapter(websocket)
        try:
            await channel._run_connection(adapter, client_id)
        except WebSocketDisconnect:
            channel._cleanup_connection(adapter)

    # -- assemble routes ----------------------------------------------------

    # Build the /api/v1/* routes from the service registry. build_api_app's
    # routes already carry the full "/api/v1/..." path, so splice them in at the
    # top level — mounting under "/api/v1" would double the prefix.
    api_app = build_api_app(registry, auth=auth, static_token=static_token)

    routes: list[Any] = [
        # WebSocket chat — listed first so the upgrade matches before any HTTP route.
        WebSocketRoute(ws_path, chat_ws_endpoint),
        # Signed session reads — must precede the generic /api/v1 routes so the
        # media-signing versions win (the generic handler can't sign).
        Route("/api/v1/sessions/{key}/messages", v1_session_messages, methods=["GET"]),
        Route("/api/v1/sessions/{key}/webui-thread", v1_webui_thread, methods=["GET"]),
        # /api/v1/* — new front door (highest priority).
        *api_app.routes,
        # /webui/bootstrap — token mint.
        Route("/webui/bootstrap", bootstrap_handler, methods=["GET"]),
        # /api/media/{sig}/{payload} — signed media.
        Route(
            "/api/media/{sig}/{payload}",
            media_handler,
            methods=["GET"],
        ),
    ]

    # SPA static files (optional).
    if resolved_static is not None and resolved_static.is_dir():
        routes.append(
            Mount("/", app=_SpaStaticFiles(directory=str(resolved_static), html=True))
        )

    return Starlette(
        routes=routes,
        middleware=[Middleware(RequestIdMiddleware)],
    )


class _SpaStaticFiles(StaticFiles):
    """StaticFiles subclass with SPA history-mode fallback.

    When a path is not found among static assets, serves ``index.html``
    instead of raising a 404, so the client-side router can handle it.
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        from starlette.exceptions import HTTPException

        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise
