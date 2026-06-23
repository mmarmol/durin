"""Service catalog — enumerates all service classes and builds a registry for spec-reading.

This module is the single place that lists every HTTP-exposed service.  The
OpenAPI generator (SP3) and any future tooling that needs the full route table
import ``SERVICE_CLASSES`` or call ``build_catalog_registry()``.

Instantiation notes
-------------------
Services are instantiated with inert ``None``/stub deps because only their
``@route`` specs are read; no service method is ever called through this
registry.  Each class's dep handling:

- ``SecretsService``   — no deps.
- ``CronService``      — ``cron_scheduler=None`` (stored, not called here).
- ``SessionsService``  — ``session_manager=None`` (stored, not called here).
- ``SettingsService``  — no deps.
- ``ConfigService``    — no deps.
- ``SkillsService``    — ``workspace=Path("/")`` (stored, never touched).
- ``MemoryService``    — ``workspace_resolver=lambda: Path("/")`` (callable, never called).
- ``McpService``        — ``mcp_runtime=None`` (config-only status; never called here).
- ``HealthService``    — no deps.
- ``CommandsService``  — no deps.
- ``OAuthService``     — no deps.
- ``AuthService``      — ``store=None`` → creates a default ``ApiTokenStore`` (safe to do).
"""

from __future__ import annotations

from pathlib import Path

from durin.service.auth import AuthService
from durin.service.commands import CommandsService
from durin.service.config import ConfigService
from durin.service.cron import CronService
from durin.service.health import HealthService
from durin.service.mcp import McpService
from durin.service.memory import MemoryService
from durin.service.oauth import OAuthService
from durin.service.registry import ServiceRegistry
from durin.service.secrets import SecretsService
from durin.service.sessions import SessionsService
from durin.service.settings import SettingsService
from durin.service.skills import SkillsService
from durin.service.workflows import WorkflowsService

SERVICE_CLASSES: list[type] = [
    SecretsService,
    CronService,
    SessionsService,
    SettingsService,
    ConfigService,
    SkillsService,
    MemoryService,
    McpService,
    HealthService,
    CommandsService,
    OAuthService,
    AuthService,
    WorkflowsService,
]


def build_catalog_registry() -> ServiceRegistry:
    """Return a ``ServiceRegistry`` with all services registered (spec-reading only).

    Each service is instantiated with inert deps — no method is ever called
    through this registry.  Use it to enumerate ``registry.routes`` for
    tooling (OpenAPI generation, doc generation, etc.).
    """
    registry = ServiceRegistry()
    registry.register("secrets", SecretsService())
    registry.register("cron", CronService(cron_scheduler=None))
    registry.register("sessions", SessionsService(session_manager=None))
    registry.register("settings", SettingsService())
    registry.register("config", ConfigService())
    registry.register("skills", SkillsService(workspace=Path("/")))
    registry.register("memory", MemoryService(workspace_resolver=lambda: Path("/")))
    registry.register("mcp", McpService())
    registry.register("health", HealthService())
    registry.register("commands", CommandsService())
    registry.register("oauth", OAuthService())
    registry.register("auth", AuthService(store=None))
    registry.register("workflows", WorkflowsService(workspace=Path("/")))
    return registry
