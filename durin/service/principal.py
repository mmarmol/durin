"""Principal — the transport-agnostic identity + authorization carried into every
service call.

The HTTP adapter builds a ``Principal`` from a verified bearer token (SP2); the
in-process TUI and cron use ``Principal.local()``. Services authorize by calling
``principal.require(Scope.X)``. Nothing here imports a transport.

The scope catalog is the single authorization vocabulary; the route table (SP3)
references these same values. ``Scope.ADMIN`` implies every other scope.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from durin.service.types import ForbiddenError, ValidationFailedError


class Scope(str, Enum):
    """Permission scopes, named ``<domain>:<read|write>``.

    The domain set mirrors the planned service classes (settings, secrets,
    skills, cron, sessions, config, memory). It is adjusted as SP1 extracts
    the real services; unused scopes are removed rather than left speculative.
    """

    ADMIN = "admin"

    SETTINGS_READ = "settings:read"
    SETTINGS_WRITE = "settings:write"
    SECRETS_READ = "secrets:read"
    SECRETS_WRITE = "secrets:write"
    SKILLS_READ = "skills:read"
    SKILLS_WRITE = "skills:write"
    CRON_READ = "cron:read"
    CRON_WRITE = "cron:write"
    SESSIONS_READ = "sessions:read"
    SESSIONS_WRITE = "sessions:write"
    CONFIG_READ = "config:read"
    CONFIG_WRITE = "config:write"
    # Speaking outward as durin, in a conversation with an external party, is a
    # different power from editing a session file — a token that may rename a
    # chat has no business posting into a customer's Slack thread.
    CHANNELS_WRITE = "channels:write"
    # Conversing with durin — the native chat routes and the OpenAI-compatible
    # /v1 endpoint — is its own power: a remote agent may hold a conversation
    # without being able to read or administer sessions, config, or secrets,
    # and it never carries a person's authority to approve privileged actions.
    CHAT_WRITE = "chat:write"
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    MCP_READ = "mcp:read"
    MCP_WRITE = "mcp:write"
    WORKFLOWS_READ = "workflows:read"
    WORKFLOWS_WRITE = "workflows:write"
    AUTOMATIONS_READ = "automations:read"
    AUTOMATIONS_WRITE = "automations:write"
    SYSTEM_READ = "system:read"
    SYSTEM_WRITE = "system:write"


# Every scope name a token may carry. Token issuing (the CLI and the API)
# validates requested scopes against this one set.
SCOPE_VALUES: frozenset[str] = frozenset(s.value for s in Scope)


def check_requested_scopes(scopes: Iterable[str]) -> None:
    """Raise :class:`ValidationFailedError` unless ``scopes`` is a valid grant.

    A valid grant names at least one scope, and only scopes in the catalog.
    Both token-issuing paths (the CLI and the API) call this one check, so they
    accept and refuse the same requests. ``details["scopes"]`` lists the
    unknown names, and is empty when no scope was named.
    """
    requested = set(scopes)
    if not requested:
        raise ValidationFailedError("at least one scope is required", details={"scopes": []})
    unknown = sorted(requested - SCOPE_VALUES)
    if unknown:
        raise ValidationFailedError(
            f"unknown scope(s): {', '.join(unknown)}", details={"scopes": unknown}
        )


def check_requested_lifetime(ttl_s: float | None) -> None:
    """Raise :class:`ValidationFailedError` unless ``ttl_s`` is a valid lifetime.

    A valid lifetime is ``None`` (the token does not expire) or a finite number
    of seconds greater than zero. NaN and the infinities are refused because an
    expiry built from them is never seen as passed (NaN compares false both
    ways), and zero or a negative number would mint a token already dead.
    Both token-issuing paths (the CLI and the API) call this one check.
    """
    if ttl_s is None:
        return
    if not math.isfinite(ttl_s) or ttl_s <= 0:
        raise ValidationFailedError(
            "ttl_s must be a finite number of seconds greater than zero"
        )


@dataclass(frozen=True)
class Principal:
    """Who is making a request, and what they are allowed to do.

    ``subject`` is the token id (remote, webui) or ``"local"`` (in-process).
    ``scopes`` is the granted scope-value set; ``Scope.ADMIN`` short-circuits
    every check. ``kind`` says what presented the request: ``"local"`` for
    in-process callers (the TUI, cron, the agent's own tools), ``"webui"`` for
    the dashboard session a person signed in to, ``"remote"`` for any other
    token (one issued for the API, or the configured static token).
    ``expires_at`` is when the presenting token stops being valid (epoch
    seconds, the token store's clock), or ``None`` for a credential that does
    not expire (the local principal, the static token, a non-expiring token).
    Frozen so it is immutable and hashable — safe to stash and pass around.
    """

    subject: str
    scopes: frozenset[str]
    kind: str  # "local" | "remote" | "webui"
    expires_at: float | None = None

    @classmethod
    def local(cls) -> "Principal":
        """The in-process principal (TUI, cron): full authority, no token."""
        return cls(subject="local", scopes=frozenset({Scope.ADMIN.value}), kind="local")

    @classmethod
    def remote(
        cls,
        subject: str,
        scopes: frozenset[str] | set[str],
        *,
        expires_at: float | None = None,
    ) -> "Principal":
        """A token-derived principal with an explicit scope grant."""
        return cls(
            subject=subject, scopes=frozenset(scopes), kind="remote", expires_at=expires_at
        )

    @classmethod
    def webui(
        cls,
        subject: str,
        scopes: frozenset[str] | set[str],
        *,
        expires_at: float | None = None,
    ) -> "Principal":
        """The dashboard session: a token ``/webui/bootstrap`` minted for the
        person signed in to the webui."""
        return cls(
            subject=subject, scopes=frozenset(scopes), kind="webui", expires_at=expires_at
        )

    def has_scope(self, scope: "str | Scope") -> bool:
        """True if the principal holds ``scope`` (or ADMIN)."""
        value = scope.value if isinstance(scope, Scope) else scope
        return Scope.ADMIN.value in self.scopes or value in self.scopes

    def require(self, scope: "str | Scope") -> None:
        """Raise :class:`ForbiddenError` if the principal lacks ``scope``."""
        if not self.has_scope(scope):
            value = scope.value if isinstance(scope, Scope) else scope
            raise ForbiddenError(f"missing required scope: {value}", details={"scope": value})
