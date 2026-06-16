"""Starlette ASGI app — the new HTTP front door (SP4).

Exports:
    resolve_principal_from_headers  — extract + verify a bearer token.
    build_api_app                   — build the read-only Starlette app.

The app mounts every GET route whose scope ends in ``:read`` from the
registry, adapts it generically (path params + query params → request
model), and maps DomainError codes to RFC-9457 problem+json responses.
Write routes (POST/PATCH/DELETE) are mounted by SP5, which reuses the
same generic adapter extended for JSON-body parsing.

The controller (durin/cli/commands.py) is responsible for:
    - building the ServiceRegistry with real deps,
    - calling build_api_app(),
    - running uvicorn.Server(Config(app, ...)).serve() inside the loop.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from durin.service.auth import AuthService
from durin.service.principal import Principal, Scope
from durin.service.registry import BoundRoute, ServiceRegistry
from durin.service.types import (
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    UnauthenticatedError,
    UnavailableError,
    ValidationFailedError,
)

# ---------------------------------------------------------------------------
# DomainError → HTTP status map
# ---------------------------------------------------------------------------

_ERROR_STATUS: dict[str, int] = {
    UnauthenticatedError.code: 401,
    ForbiddenError.code: 403,
    NotFoundError.code: 404,
    ConflictError.code: 409,
    ValidationFailedError.code: 422,
    UnavailableError.code: 503,
}

_ERROR_TITLE: dict[str, str] = {
    UnauthenticatedError.code: "Unauthenticated",
    ForbiddenError.code: "Forbidden",
    NotFoundError.code: "Not Found",
    ConflictError.code: "Conflict",
    ValidationFailedError.code: "Unprocessable Entity",
    UnavailableError.code: "Service Unavailable",
}


def _problem_response(err: DomainError) -> Response:
    """Map a DomainError to an RFC-9457 application/problem+json response."""
    status = _ERROR_STATUS.get(err.code, 500)
    title = _ERROR_TITLE.get(err.code, "Internal Server Error")
    body = json.dumps(
        {
            "type": f"urn:durin:error:{err.code}",
            "title": title,
            "status": status,
            "detail": err.message,
        }
    )
    return Response(body, status_code=status, media_type="application/problem+json")


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
    if static_token and token == static_token:
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


def _build_handler(
    bound: BoundRoute,
    *,
    auth: AuthService,
    static_token: str,
) -> Callable:
    """Return an async Starlette handler for a single BoundRoute.

    Path params and query params (including multi-value lists via .getlist)
    are merged by field name and passed to the request_model constructor.
    The handler:
      1. Resolves the principal (401 if missing).
      2. Builds the request model from params (422 on ValidationError).
      3. Awaits the service handler (DomainError → problem+json).
      4. Returns JSONResponse(result.model_dump(by_alias=True)).
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
                body = json.dumps(
                    {
                        "type": "urn:durin:error:validation_failed",
                        "title": "Unprocessable Entity",
                        "status": 422,
                        "detail": exc.errors(),
                    }
                )
                return Response(body, status_code=422, media_type="application/problem+json")
        else:
            model = None

        try:
            if model is not None:
                result = await handler(model, principal)
            else:
                result = await handler(principal)
        except DomainError as exc:
            return _problem_response(exc)

        return JSONResponse(result.model_dump(by_alias=True))

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
    """Build the read-only Starlette ASGI app.

    Mounts:
    - ``GET /api/v1/health``    — unauthenticated liveness probe.
    - One Starlette Route per GET ``@route`` whose scope ends in ``:read``.

    Write routes (POST/PATCH/DELETE) are mounted by SP5.

    Args:
        registry:     ServiceRegistry with all services registered.
        auth:         AuthService used to resolve bearer tokens.
        static_token: If non-empty, this plaintext token grants ADMIN access
                      (the bootstrap/static token from gateway config).
    """
    routes: list[Route] = [
        Route("/api/v1/health", _health_handler, methods=["GET"]),
    ]

    for bound in registry.routes:
        if not _is_read_route(bound):
            continue
        handler = _build_handler(bound, auth=auth, static_token=static_token)
        routes.append(Route(bound.spec.path, handler, methods=["GET"]))

    return Starlette(
        routes=routes,
        middleware=[Middleware(RequestIdMiddleware)],
    )


async def _health_handler(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})
