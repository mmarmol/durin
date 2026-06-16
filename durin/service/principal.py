"""Principal — the transport-agnostic identity + authorization carried into every
service call.

The HTTP adapter builds a ``Principal`` from a verified bearer token (SP2); the
in-process TUI and cron use ``Principal.local()``. Services authorize by calling
``principal.require(Scope.X)``. Nothing here imports a transport.

The scope catalog is the single authorization vocabulary; the route table (SP3)
references these same values. ``Scope.ADMIN`` implies every other scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from durin.service.types import ForbiddenError


class Scope(str, Enum):
    """Permission scopes, named ``<domain>:<read|write>``.

    The domain set mirrors the planned service classes (settings, secrets,
    skills, cron, sessions, config, memory, chat). It is adjusted as SP1 extracts
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
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    CHAT_READ = "chat:read"
    CHAT_WRITE = "chat:write"
    SYSTEM_READ = "system:read"
    SYSTEM_WRITE = "system:write"


@dataclass(frozen=True)
class Principal:
    """Who is making a request, and what they are allowed to do.

    ``subject`` is the token id (remote) or ``"local"`` (in-process). ``scopes``
    is the granted scope-value set; ``Scope.ADMIN`` short-circuits every check.
    Frozen so it is immutable and hashable — safe to stash and pass around.
    """

    subject: str
    scopes: frozenset[str]
    kind: str  # "local" | "remote"

    @classmethod
    def local(cls) -> "Principal":
        """The in-process principal (TUI, cron): full authority, no token."""
        return cls(subject="local", scopes=frozenset({Scope.ADMIN.value}), kind="local")

    @classmethod
    def remote(cls, subject: str, scopes: frozenset[str] | set[str]) -> "Principal":
        """A token-derived principal with an explicit scope grant."""
        return cls(subject=subject, scopes=frozenset(scopes), kind="remote")

    def has_scope(self, scope: "str | Scope") -> bool:
        """True if the principal holds ``scope`` (or ADMIN)."""
        value = scope.value if isinstance(scope, Scope) else scope
        return Scope.ADMIN.value in self.scopes or value in self.scopes

    def require(self, scope: "str | Scope") -> None:
        """Raise :class:`ForbiddenError` if the principal lacks ``scope``."""
        if not self.has_scope(scope):
            value = scope.value if isinstance(scope, Scope) else scope
            raise ForbiddenError(f"missing required scope: {value}", details={"scope": value})
