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
    loops_runtime: Any = None,
    tool_registry_resolver: Callable[[], Any] | None = None,
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

    ``tool_registry_resolver`` is optional: the unified gateway passes
    ``lambda: agent.tools`` so the modes service's tool catalog reflects exactly
    what the running agent can call; surfaces without a loop leave it ``None`` and
    the catalog falls back to loader discovery (core built-ins only).

    ``loops_runtime`` is optional: the unified gateway passes the live
    ``LoopsRuntime`` so ``LoopsService`` can fire/answer runs; surfaces
    without one leave it ``None`` and those two routes report unavailable.
    """
    from durin.security.api_tokens import ApiTokenStore
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
        loops_runtime=loops_runtime,
    )
    registry.register("secrets", SecretsService())
    registry.register("cron", CronService(cron_scheduler=cron_service))
    registry.register("sessions", SessionsService(session_manager=session_manager))
    registry.register("settings", SettingsService(on_default_changed=on_default_changed))
    registry.register("config", ConfigService(on_config_changed=on_config_changed))
    registry.register("telegram", TelegramService())
    registry.register("discord", DiscordService())
    registry.register("slack", SlackService())
    registry.register("whatsapp", WhatsAppService())
    registry.register("channels_runtime", ChannelsRuntimeService(channel_manager=channel_manager))
    registry.register("skills", SkillsService(workspace=_workspace()))
    registry.register("memory", MemoryService(workspace_resolver=_workspace))
    registry.register("personas", PersonasService(workspace_resolver=_workspace, on_config_changed=on_config_changed))
    registry.register("mcp", McpService(mcp_runtime=mcp_runtime))
    registry.register("health", HealthService(
        channel_manager=channel_manager, cron_service=cron_service))
    registry.register("commands", CommandsService())
    registry.register("modes", ModesService(tool_registry_resolver=tool_registry_resolver))
    registry.register("oauth", OAuthService())
    registry.register("auth", AuthService(ApiTokenStore()))
    registry.register("workflows", WorkflowsService(
        workspace=_workspace(), app_config=config, sessions=session_manager))
    from durin.service.tasks import TasksService
    registry.register("tasks", TasksService(
        workspace=_workspace(), subagent_manager=subagent_manager,
        sessions=session_manager))
    registry.register("loops", LoopsService(
        workspace=_workspace(), cron_service=cron_service, runtime=loops_runtime,
        hooks_secret=lambda: ApiTokenStore().get_or_create_hooks_secret()))

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

    # Same crash recovery for loop runs: a gateway restart mid-run otherwise
    # leaves a "running" manifest forever, and a concurrency="single" loop
    # never fires again (its active_runs check sees the stale manifest).
    try:
        import time

        from durin.loops import run_log as loops_run_log

        loops_run_log.reconcile_running(_workspace(), now=time.time())
    except Exception:  # noqa: BLE001 - crash reconciliation is best-effort
        pass

    # Sweep stale claims (a thread-to-waiting-run mapping released on the
    # normal answer path) that were never released, e.g. the process died
    # before a run reached waiting_info's release or the counterpart just
    # never replied. Claims are conversation-scoped, not tied to any
    # queue_ttl_s config knob, so a flat week-long constant bounds them
    # instead.
    try:
        from durin.loops import claims as loops_claims

        loops_claims.prune(_workspace(), max_age_s=7 * 24 * 3600)
    except Exception:  # noqa: BLE001 - best-effort sweep
        pass
    return registry
