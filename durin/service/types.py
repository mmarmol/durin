"""Transport-agnostic DTO bases and the domain error hierarchy for the service layer.

These types are the vocabulary every service method speaks: a method takes a
``Command`` or ``Query`` (validated input) plus a ``Principal`` (who is asking)
and returns a ``Result`` (or raises a ``DomainError``). Nothing here imports
HTTP/WS — adapters map ``DomainError.code`` to their own status vocabulary.

Error classes carry the ``Error`` suffix to match durin's convention
(``SecretError``, ``IngestError``, …) and satisfy ruff N818.

Nothing here imports HTTP/WS — adapters map ``DomainError.code`` to their own status vocabulary.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ServiceModel(BaseModel):
    """Base for every service DTO.

    Matches durin's existing ``Base`` convention (``durin/config/schema.py``):
    camelCase wire aliases via ``to_camel`` while still accepting snake_case
    field names. That lets the TypeScript clients speak camelCase and the Python
    callers speak snake_case off the same model.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class Command(ServiceModel):
    """Validated input to a *mutating* service method.

    ``extra="forbid"`` rejects unknown fields so a client typo or an injected
    extra key surfaces as a validation error instead of being silently dropped.
    """

    model_config = ConfigDict(extra="forbid")


class Query(ServiceModel):
    """Validated input to a *read-only* service method. Rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")


class Result(ServiceModel):
    """A service method's return value.

    Unlike ``Command``/``Query`` it does not forbid extras: results are built in
    code (not parsed from an untrusted wire) and stay forward-compatible as new
    fields are added.
    """


class DomainError(Exception):
    """Base for service-layer errors.

    Transport-agnostic: each adapter maps ``code`` to its own status vocabulary
    (an HTTP status, a WS error frame, a CLI exit code…). ``details`` carries
    structured context an adapter can echo back to the caller.
    """

    code = "error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}


class UnauthenticatedError(DomainError):
    """No valid credential was presented (maps to HTTP 401)."""

    code = "unauthenticated"


class ForbiddenError(DomainError):
    """Authenticated, but the principal lacks the required scope (HTTP 403)."""

    code = "forbidden"


class NotFoundError(DomainError):
    """The addressed resource does not exist (HTTP 404)."""

    code = "not_found"


class ConflictError(DomainError):
    """The request conflicts with current state (HTTP 409)."""

    code = "conflict"


class ValidationFailedError(DomainError):
    """The request was well-formed but semantically invalid (HTTP 422).

    Named ``ValidationFailedError`` (not ``ValidationError``) to avoid colliding
    with ``pydantic.ValidationError``, which adapters also handle.
    """

    code = "validation_failed"


class TooManyRequestsError(DomainError):
    """The caller exceeded a rate or quantity limit (HTTP 429)."""

    code = "too_many_requests"


class UnavailableError(DomainError):
    """A dependency needed to serve the request is not available (HTTP 503)."""

    code = "unavailable"
