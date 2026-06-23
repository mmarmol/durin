"""Build the service registry with real gateway dependencies.

Shared by the websocket channel (bootstrap, the secret-store frame, media) and
the unified Starlette front door so both surfaces serve the SAME service set,
wired to the same ``session_manager`` / ``cron_service`` / ``config``. The
``catalog`` builds a deps-less registry for spec-reading; this one is the
functional, dependency-wired registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from durin.service.registry import ServiceRegistry


def build_service_registry(
    *,
    config: Any,
    session_manager: Any = None,
    cron_service: Any = None,
    bus: Any = None,
    mcp_runtime: Any = None,
) -> ServiceRegistry:
    """Construct a registry with all domain services wired to real deps.

    ``mcp_runtime`` (a :class:`~durin.agent.mcp_runtime.McpRuntime`) is optional:
    the unified gateway passes one built from its live ``AgentLoop`` so MCP status
    is live; surfaces without a loop (the websocket channel's shim registry) leave
    it ``None`` and the MCP service reports config-only status.
    """
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.commands import CommandsService
    from durin.service.config import ConfigService
    from durin.service.cron import CronService
    from durin.service.health import HealthService
    from durin.service.mcp import McpService
    from durin.service.memory import MemoryService
    from durin.service.oauth import OAuthService
    from durin.service.secrets import SecretsService
    from durin.service.sessions import SessionsService
    from durin.service.settings import SettingsService
    from durin.service.skills import SkillsService
    from durin.service.workflows import WorkflowsService

    def _workspace() -> Path:
        # Mirror WebSocketChannel._endpoint_workspace: a --workspace override
        # lives on the session manager; fall back to the config file otherwise.
        if session_manager is not None:
            return session_manager.workspace
        from durin.config.loader import load_config

        return load_config().workspace_path

    registry = ServiceRegistry(
        config=config,
        session_manager=session_manager,
        cron_service=cron_service,
        bus=bus,
    )
    registry.register("secrets", SecretsService())
    registry.register("cron", CronService(cron_scheduler=cron_service))
    registry.register("sessions", SessionsService(session_manager=session_manager))
    registry.register("settings", SettingsService())
    registry.register("config", ConfigService())
    registry.register("skills", SkillsService(workspace=_workspace()))
    registry.register("memory", MemoryService(workspace_resolver=_workspace))
    registry.register("mcp", McpService(mcp_runtime=mcp_runtime))
    registry.register("health", HealthService())
    registry.register("commands", CommandsService())
    registry.register("oauth", OAuthService())
    registry.register("auth", AuthService(ApiTokenStore()))
    registry.register("workflows", WorkflowsService(workspace=_workspace()))
    return registry
