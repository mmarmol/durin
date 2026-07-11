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
- ``PersonasService``  — ``workspace_resolver=lambda: Path("/")`` (callable, never called).
- ``McpService``        — ``mcp_runtime=None`` (config-only status; never called here).
- ``HealthService``    — no deps.
- ``CommandsService``  — no deps.
- ``OAuthService``     — no deps.
- ``AuthService``      — ``store=None`` → creates a default ``ApiTokenStore`` (safe to do).
- ``TasksService``     — ``workspace=Path("/")`` (stored, never touched), ``subagent_manager=None``.
- ``LoopsService``     — ``workspace=Path("/")``, ``cron_service=None``, ``runtime=None`` (stored, never called here).
- ``DiscordService``       — no deps.
- ``TelegramService``      — no deps.
- ``SlackService``         — no deps (slack_sdk imported lazily per call).
- ``ChannelsRuntimeService`` — ``channel_manager=None`` (stored, never called here).
"""

from __future__ import annotations

from pathlib import Path

from durin.service.auth import AuthService
from durin.service.channels_discord import DiscordService
from durin.service.channels_runtime import ChannelsRuntimeService
from durin.service.channels_slack import SlackService
from durin.service.channels_telegram import TelegramService
from durin.service.channels_whatsapp import WhatsAppService
from durin.service.commands import CommandsService
from durin.service.config import ConfigService
from durin.service.cron import CronService
from durin.service.health import HealthService
from durin.service.loops import LoopsService
from durin.service.mcp import McpService
from durin.service.memory import MemoryService
from durin.service.modes import ModesService
from durin.service.oauth import OAuthService
from durin.service.personas import PersonasService
from durin.service.registry import ServiceRegistry
from durin.service.secrets import SecretsService
from durin.service.sessions import SessionsService
from durin.service.settings import SettingsService
from durin.service.skills import SkillsService
from durin.service.tasks import TasksService
from durin.service.workflows import WorkflowsService

SERVICE_CLASSES: list[type] = [
    SecretsService,
    CronService,
    SessionsService,
    SettingsService,
    ConfigService,
    SkillsService,
    MemoryService,
    PersonasService,
    McpService,
    HealthService,
    CommandsService,
    ModesService,
    OAuthService,
    AuthService,
    TasksService,
    WorkflowsService,
    LoopsService,
    DiscordService,
    TelegramService,
    SlackService,
    WhatsAppService,
    ChannelsRuntimeService,
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
    registry.register("personas", PersonasService(workspace_resolver=lambda: Path("/")))
    registry.register("mcp", McpService())
    registry.register("health", HealthService())
    registry.register("commands", CommandsService())
    registry.register("modes", ModesService())
    registry.register("oauth", OAuthService())
    registry.register("auth", AuthService(store=None))
    registry.register("tasks", TasksService(workspace=Path("/")))
    registry.register("workflows", WorkflowsService(workspace=Path("/")))
    registry.register("loops", LoopsService(workspace=Path("/")))
    registry.register("telegram", TelegramService())
    registry.register("discord", DiscordService())
    registry.register("slack", SlackService())
    registry.register("whatsapp", WhatsAppService())
    registry.register("channels_runtime", ChannelsRuntimeService())
    return registry
