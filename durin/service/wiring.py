"""Build the service registry with real gateway dependencies.

Shared by the websocket channel (bootstrap, the secret-store frame, media) and
the unified Starlette front door so both surfaces serve the SAME service set,
wired to the same ``session_manager`` / ``cron_service`` / ``config``. The
``catalog`` builds a deps-less registry for spec-reading; this one is the
functional, dependency-wired registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from durin.service.registry import ServiceRegistry


def build_service_registry(
    *,
    config: Any,
    session_manager: Any = None,
    cron_service: Any = None,
    bus: Any = None,
    mcp_runtime: Any = None,
    subagent_manager: Any = None,
    channel_manager: Any = None,
    on_config_changed: Callable[[], None] | None = None,
    on_default_changed: Callable[[], None] | None = None,
) -> ServiceRegistry:
    """Construct a registry with all domain services wired to real deps.

    ``mcp_runtime`` (a :class:`~durin.agent.mcp_runtime.McpRuntime`) is optional:
    the unified gateway passes one built from its live ``AgentLoop`` so MCP status
    is live; surfaces without a loop (the websocket channel's shim registry) leave
    it ``None`` and the MCP service reports config-only status.

    ``subagent_manager`` is optional: the unified gateway passes the live
    ``agent.subagents`` instance so the Tasks service can report running sub-agents;
    the websocket channel's shim registry passes ``None`` and only workflow runs are
    reported.

    ``channel_manager`` is optional: the unified gateway passes the live
    ``ChannelManager`` so the channels-runtime service can hot-start/stop channels;
    the websocket channel's shim registry passes ``None`` and those routes report
    "channel_manager not available".

    ``on_default_changed`` is optional: the unified gateway passes the live
    ``AgentLoop.apply_default_model_live`` so a default model/provider change made
    through the settings service applies to the running loop without a restart;
    surfaces without a loop leave it ``None`` and the change applies on next start.
    """
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.channels_runtime import ChannelsRuntimeService
    from durin.service.channels_telegram import TelegramService
    from durin.service.commands import CommandsService
    from durin.service.config import ConfigService
    from durin.service.cron import CronService
    from durin.service.health import HealthService
    from durin.service.mcp import McpService
    from durin.service.memory import MemoryService
    from durin.service.modes import ModesService
    from durin.service.oauth import OAuthService
    from durin.service.personas import PersonasService
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
        channel_manager=channel_manager,
    )
    registry.register("secrets", SecretsService())
    registry.register("cron", CronService(cron_scheduler=cron_service))
    registry.register("sessions", SessionsService(session_manager=session_manager))
    registry.register("settings", SettingsService(on_default_changed=on_default_changed))
    registry.register("config", ConfigService(on_config_changed=on_config_changed))
    registry.register("telegram", TelegramService())
    registry.register("channels_runtime", ChannelsRuntimeService(channel_manager=channel_manager))
    registry.register("skills", SkillsService(workspace=_workspace()))
    registry.register("memory", MemoryService(workspace_resolver=_workspace))
    registry.register("personas", PersonasService(workspace_resolver=_workspace, on_config_changed=on_config_changed))
    registry.register("mcp", McpService(mcp_runtime=mcp_runtime))
    registry.register("health", HealthService())
    registry.register("commands", CommandsService())
    registry.register("modes", ModesService())
    registry.register("oauth", OAuthService())
    registry.register("auth", AuthService(ApiTokenStore()))
    registry.register("workflows", WorkflowsService(
        workspace=_workspace(), app_config=config, sessions=session_manager))
    from durin.service.tasks import TasksService
    registry.register("tasks", TasksService(
        workspace=_workspace(), subagent_manager=subagent_manager,
        sessions=session_manager))

    # Crash recovery: the gateway is the long-lived process, so its boot is the natural
    # point to reconcile run manifests still "running" from a previous process that died
    # before finalizing them. Best-effort — a sweep failure must not block startup.
    try:
        import time

        from durin.workflow import run_log

        run_log.reconcile_running(
            _workspace(), now=time.time(), max_age_s=run_log.RECONCILE_AGE_S)
    except Exception:  # noqa: BLE001 - crash reconciliation is best-effort
        pass
    return registry
