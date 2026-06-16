"""durin.service — the transport-agnostic service core.

Every gateway capability is a service method taking a validated ``Command`` or
``Query`` plus a ``Principal`` and returning a ``Result`` (or raising a
``DomainError``). The HTTP/WS adapters and the in-process TUI all call the same
methods. See ``docs/architecture/api.md``.
"""

from durin.service.principal import Principal, Scope
from durin.service.registry import BoundRoute, RouteSpec, ServiceRegistry, route
from durin.service.types import (
    Command,
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    Query,
    Result,
    ServiceModel,
    UnauthenticatedError,
    UnavailableError,
    ValidationFailedError,
)

__all__ = [
    # identity & authorization
    "Principal",
    "Scope",
    # registry & routing
    "ServiceRegistry",
    "RouteSpec",
    "BoundRoute",
    "route",
    # DTO bases
    "ServiceModel",
    "Command",
    "Query",
    "Result",
    # error hierarchy
    "DomainError",
    "UnauthenticatedError",
    "ForbiddenError",
    "NotFoundError",
    "ConflictError",
    "ValidationFailedError",
    "UnavailableError",
]
