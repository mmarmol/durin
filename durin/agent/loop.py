"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
from contextlib import AsyncExitStack, nullcontext, suppress
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from durin.agent import model_presets as preset_helpers
from durin.agent.context import ContextBuilder
from durin.extras import ensure_or_note
from durin.agent.hook import AgentHook, CompositeHook
from durin.agent.memory import Consolidator
from durin.agent.progress_hook import AgentProgressHook
from durin.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec
from durin.agent.skill_usage import extract_skill_calls
from durin.agent.subagent import SubagentManager
from durin.agent.tools.context import AuxProviderHandle
from durin.agent.tools.file_state import FileStateStore, bind_file_states, reset_file_states
from durin.agent.tools.message import MessageTool
from durin.agent.tools.registry import ToolRegistry
from durin.agent.tools.self import MyTool
from durin.bus.events import OUTBOUND_META_AGENT_UI, InboundMessage, OutboundMessage
from durin.bus.queue import MessageBus
from durin.command import CommandContext, CommandRouter, register_builtin_commands
from durin.config.schema import AgentDefaults, ModelPresetConfig
from durin.providers.base import LLMProvider
from durin.providers.factory import ProviderSnapshot
from durin.session.goal_state import (
    goal_state_ws_blob,
    runner_wall_llm_timeout_s,
)
from durin.session.manager import Session, SessionManager
from durin.utils.document import extract_documents
from durin.utils.helpers import image_placeholder_text
from durin.utils.helpers import truncate_text as truncate_text_fn
from durin.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE
from durin.utils.session_attachments import merge_turn_media_into_last_assistant
from durin.utils.webui_titles import mark_webui_session, maybe_generate_webui_title_after_turn
from durin.utils.webui_turn_helpers import publish_turn_run_status

# Tools whose output is error-dense at the END of the buffer (build
# logs, shell stderr/stdout, test runners). For these we truncate from
# the head so the model sees the tail. Everything else (file reads,
# searches, web fetches) keeps the default head-keep behaviour because
# the leading context is the relevant part. Inspired by pi's per-tool
# truncation direction policy.
_TAIL_TRUNCATION_TOOLS = frozenset({"exec", "shell"})


def _truncate_tool_output(content: str, max_chars: int, tool_name: str | None) -> str:
    """Truncate ``content`` choosing direction based on the tool name."""
    direction = "tail" if (tool_name in _TAIL_TRUNCATION_TOOLS) else "head"
    return truncate_text_fn(content, max_chars, direction=direction)


if TYPE_CHECKING:
    from durin.config.schema import (
        ChannelsConfig,
        ToolsConfig,
    )
    from durin.cron.service import CronService


UNIFIED_SESSION_KEY = "unified:default"


class TurnState(Enum):
    RESTORE = auto()
    COMPACT = auto()
    COMMAND = auto()
    BUILD = auto()
    RUN = auto()
    SAVE = auto()
    RESPOND = auto()
    DONE = auto()


@dataclass
class StateTraceEntry:
    state: TurnState
    started_at: float
    duration_ms: float
    event: str
    error: str | None = None


def _build_aux_providers(config: Any) -> dict[str, AuxProviderHandle]:
    """Construct one provider per configured auxiliary modality.

    Called once during ``AgentLoop.from_config``. For each entry in
    ``config.agents.aux_models`` (vision / audio / …) we resolve the
    referenced preset or build an inline ``ModelPresetConfig`` and
    call :func:`make_provider`. The resulting providers are reused for
    every bridge-tool invocation — credentials and HTTP clients open
    once at startup.

    Returns an empty dict when no aux models are configured; bridge
    tools then see no entry and stay hidden from the LLM's tool list.
    """
    from durin.config.schema import ModelPresetConfig
    from durin.providers.factory import make_provider

    out: dict[str, AuxProviderHandle] = {}
    aux = getattr(getattr(config, "agents", None), "aux_models", None)
    if aux is None:
        return out
    for kind in ("vision", "audio", "memory"):
        entry = getattr(aux, kind, None)
        if entry is None:
            continue
        preset: ModelPresetConfig
        if entry.preset:
            try:
                preset = config.resolve_preset(entry.preset)
            except Exception:
                logger.exception("Failed to resolve aux preset {!r} for {} bridge", entry.preset, kind)
                continue
        elif entry.model:
            preset = ModelPresetConfig(
                model=entry.model,
                provider=entry.provider or "auto",
            )
        else:
            continue
        try:
            provider = make_provider(config, preset=preset)
        except Exception:
            logger.exception("Failed to build aux provider for {} bridge (model={}, provider={})",
                             kind, preset.model, preset.provider)
            continue
        out[kind] = AuxProviderHandle(provider=provider, model=preset.model)
        logger.info("Aux {} bridge enabled — model={} provider={}",
                    kind, preset.model, preset.provider)
    return out


@dataclass
class TurnContext:
    msg: InboundMessage
    session_key: str
    state: TurnState
    turn_id: str
    session: Session | None = None

    history: list[dict[str, Any]] = field(default_factory=list)
    initial_messages: list[dict[str, Any]] = field(default_factory=list)

    final_content: str | None = None
    tools_used: list[str] = field(default_factory=list)
    all_messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    had_injections: bool = False
    tool_events: list[dict[str, Any]] = field(default_factory=list)

    user_persisted_early: bool = False
    save_skip: int = 0

    outbound: OutboundMessage | None = None

    on_progress: Callable[..., Awaitable[None]] | None = None
    on_stream: Callable[[str], Awaitable[None]] | None = None
    on_stream_end: Callable[..., Awaitable[None]] | None = None
    on_retry_wait: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None

    pending_queue: asyncio.Queue | None = None
    pending_summary: str | None = None

    turn_wall_started_at: float = field(default_factory=time.time)
    turn_latency_ms: int | None = None

    trace: list[StateTraceEntry] = field(default_factory=list)


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    @property
    def current_iteration(self) -> int:
        return self._current_iteration

    def _on_iteration(self, iteration: int) -> None:
        """Single callback for per-turn iteration updates.

        Audit F20 (2026-05-28): also pushes the counter into the bound
        TelemetryLogger so `emit_tool_event` can auto-inject
        ``iteration`` into every event payload (doc 07 §4.1). Pre-F20
        the field was declared NotRequired in the TypedDicts but no
        callsite ever populated it.
        """
        self._current_iteration = iteration
        try:
            from durin.telemetry.logger import current_telemetry
            tel = current_telemetry()
            if tel is not None:
                tel.set_iteration(iteration)
        except Exception:  # noqa: BLE001
            # Telemetry must never break the agent loop. Best-effort.
            pass

    @property
    def tool_names(self) -> list[str]:
        return self.tools.tool_names

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"

    # Event-driven state transition table.
    # Handlers return an event string; the driver looks up the next state here.
    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.COMMAND,
        (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
        (TurnState.COMMAND, "shortcut"): TurnState.DONE,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        consolidation_ratio: float = 0.5,
        preemptive_compact_ratio: float = 0.5,
        max_messages: int = 120,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        aux_providers: dict[str, "AuxProviderHandle"] | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        provider_signature: tuple[object, ...] | None = None,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        model_preset: str | None = None,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None = None,
        runtime_model_publisher: Callable[[str, str | None], None] | None = None,
        app_config: Any | None = None,
    ):
        from durin.config.schema import ToolsConfig

        _tc = tools_config or ToolsConfig()
        # Full DurinConfig — needed by memory_* tools that read
        # ``app_config.memory.{enabled,embedding,dream}``. Test paths that
        # construct AgentLoop directly with tools_config and no full
        # config get ``None``; the affected tools treat that as
        # memory-disabled (fall back to grep / skip vector).
        self.app_config = app_config
        defaults = AgentDefaults()
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self._provider_snapshot_loader = provider_snapshot_loader
        self._preset_snapshot_loader = preset_snapshot_loader
        self._runtime_model_publisher = runtime_model_publisher
        self._provider_signature = provider_signature
        self._default_selection_signature = preset_helpers.default_selection_signature(provider_signature)
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = (
            tool_hint_max_length if tool_hint_max_length is not None
            else defaults.tool_hint_max_length
        )
        self.tools_config = _tc
        self.web_config = _tc.web
        self.exec_config = _tc.exec
        # Aux providers for capability bridges (vision/audio/...). Empty
        # dict ⇒ no bridge tools register. Built upstream by
        # ``from_config`` from ``config.agents.aux_models``; tests can
        # inject a custom dict directly.
        self._aux_providers: dict[str, AuxProviderHandle] = dict(aux_providers or {})
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._pending_turn_latency_ms: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = hooks or []

        self.context = ContextBuilder(workspace, timezone=timezone, disabled_skills=disabled_skills)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        self.runner = AgentRunner(provider)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            tools_config=_tc,
            max_tool_result_chars=self.max_tool_result_chars,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            max_iterations=self.max_iterations,
            llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(self.sessions, sk),
            sessions=self.sessions,
        )
        self._unified_session = unified_session
        self._max_messages = max_messages if max_messages > 0 else 120
        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stacks: dict[str, AsyncExitStack] = {}
        self._mcp_connected = False
        self._mcp_connecting = False
        # Last-known telemetry snapshots, cached in-memory so /status
        # and the persistent footer can read them without going to the
        # JSONL file. Both are the most recent payload of their event;
        # ``None`` until the first event of that kind fires.
        self._last_context_composition: dict[str, Any] | None = None
        self._last_cache_usage: dict[str, Any] | None = None
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._background_tasks: list[asyncio.Task] = []
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Per-session pending queues for mid-turn message injection.
        # When a session has an active task, new messages for that session
        # are routed here instead of creating a new task.
        self._pending_queues: dict[str, asyncio.Queue] = {}
        # DURIN_MAX_CONCURRENT_REQUESTS: <=0 means unlimited; default 3.
        _max = int(os.environ.get("DURIN_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
            consolidation_ratio=consolidation_ratio,
            preemptive_compact_ratio=preemptive_compact_ratio,
        )
        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._active_preset: str | None = None
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)
        self._register_default_tools()
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)
        # Doc 25 §2.A.1 β.2: session-close trigger hook. The wiring
        # layer (``cli/commands.py``) sets this when
        # ``memory.dream.on_session_close`` is enabled — fires the
        # entity-centric dream once per session-close event (``/new``
        # archives the prior session before starting a fresh one).
        # Independent of ``Consolidator.on_post_compaction``: a user
        # may want post-compaction off (too aggressive) but
        # session-close on (low frequency, safe).
        self.on_session_close: Callable[[str], None] | None = None

        # A11 (2026-05-28) — wire memory background services. Both
        # default ON; opt-out per `cfg.memory.{file_watcher,
        # health_check}.enabled`. Failures here are isolated — the
        # agent loop still functions; only the optional background
        # work is skipped.
        self._memory_file_watcher: Any | None = None
        self._memory_health_scheduler: Any | None = None
        self._start_memory_background_services()

    # ------------------------------------------------------------------
    # A11 — memory background services lifecycle
    # ------------------------------------------------------------------

    def _start_memory_background_services(self) -> None:
        """Start the optional memory file watcher and health-check
        scheduler if the config enables them, and ensure the workspace
        has a `VAULT_README.md` for human consumers (Obsidian users,
        webui MemoryGraphView, anyone browsing files directly).

        Each service is constructed and started independently; a
        failure in one doesn't affect the other. Tests that build
        an AgentLoop with ``app_config=None`` (most of them) get
        no services — keeps test isolation tight.
        """
        if self.app_config is None:
            return
        mem_cfg = getattr(self.app_config, "memory", None)
        if mem_cfg is None:
            return

        # P9 (2026-05-30): write the vault README at workspace root if
        # missing, plus per-class `_INDEX.md` navigation helpers inside
        # each `memory/<class>/`. Idempotent + safe — never overwrites.
        # Files starting with `_` are skipped by `walk_memory()` so the
        # indices are NOT picked up as memory entries.
        try:
            from durin.memory.vault_readme import (
                ensure_class_indices,
                ensure_vault_readme,
            )

            ensure_vault_readme(self.workspace)
            ensure_class_indices(self.workspace)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "vault navigation files init failed (continuing): {}", exc,
            )

        fw_cfg = getattr(mem_cfg, "file_watcher", None)
        if fw_cfg is not None and getattr(fw_cfg, "enabled", False):
            try:
                from durin.memory.file_watcher import MemoryFileWatcher

                # N2: pass the embedding model so the watcher re-embeds entity
                # pages reactively (FTS alone leaves them vector-stale).
                _emb_model = None
                try:
                    if mem_cfg.enabled:
                        _emb_model = mem_cfg.embedding.model
                except (AttributeError, TypeError):
                    _emb_model = None
                watcher = MemoryFileWatcher(self.workspace, embedding_model=_emb_model)
                watcher.start()
                self._memory_file_watcher = watcher
                logger.info(
                    "memory file watcher started for {}", self.workspace,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "memory file watcher failed to start "
                    "(continuing without): {}", exc,
                )

        hc_cfg = getattr(mem_cfg, "health_check", None)
        if hc_cfg is not None and getattr(hc_cfg, "enabled", False):
            try:
                from durin.memory.health_check import (
                    HealthChecker,
                    HealthCheckScheduler,
                )

                checker = HealthChecker(self.workspace)
                scheduler = HealthCheckScheduler(
                    checker,
                    interval_seconds=int(
                        getattr(hc_cfg, "interval_seconds", 900),
                    ),
                )
                scheduler.start()
                self._memory_health_scheduler = scheduler
                logger.info(
                    "memory health check scheduler started "
                    "(interval={}s)",
                    getattr(hc_cfg, "interval_seconds", 900),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "memory health check failed to start "
                    "(continuing without): {}", exc,
                )

    def _stop_memory_background_services(self) -> None:
        """Stop the memory background services. Safe to call when
        they were never started — each is None-guarded."""
        watcher = self._memory_file_watcher
        if watcher is not None:
            try:
                watcher.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "memory file watcher stop raised: {}", exc,
                )
            self._memory_file_watcher = None

        scheduler = self._memory_health_scheduler
        if scheduler is not None:
            try:
                scheduler.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "memory health scheduler stop raised: {}", exc,
                )
            self._memory_health_scheduler = None

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """Create an AgentLoop from config with the common parameter set.

        Extra keyword arguments are forwarded to ``AgentLoop.__init__``,
        allowing callers to override or extend the standard config-derived
        parameters (e.g. ``cron_service``, ``session_manager``).
        """
        from durin.providers.factory import make_provider

        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        provider = extra.pop("provider", None) or make_provider(config)
        resolved = config.resolve_preset()
        model = extra.pop("model", None) or resolved.model
        context_window_tokens = extra.pop("context_window_tokens", None) or resolved.context_window_tokens
        provider_snapshot_loader = extra.pop("provider_snapshot_loader", None)
        preset_snapshot_loader = extra.pop("preset_snapshot_loader", None) or preset_helpers.make_preset_snapshot_loader(
            config,
            provider_snapshot_loader,
        )

        existing_hooks = extra.pop("hooks", None) or []
        session_key = extra.pop("session_key", None)
        hooks = list(existing_hooks)

        if session_key and hasattr(provider, "set_telemetry"):
            from durin.telemetry.logger import get_session_logger
            try:
                provider.set_telemetry(get_session_logger(session_key))
            except Exception:
                pass

        aux_providers = _build_aux_providers(config)

        return cls(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=model,
            max_iterations=defaults.max_tool_iterations,
            context_window_tokens=context_window_tokens,
            context_block_limit=defaults.context_block_limit,
            max_tool_result_chars=defaults.max_tool_result_chars,
            provider_retry_mode=defaults.provider_retry_mode,
            tool_hint_max_length=defaults.tool_hint_max_length,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            mcp_servers=config.tools.mcp_servers,
            channels_config=config.channels,
            timezone=defaults.timezone,
            unified_session=defaults.unified_session,
            disabled_skills=defaults.disabled_skills,
            consolidation_ratio=defaults.consolidation_ratio,
            preemptive_compact_ratio=defaults.preemptive_compact_ratio,
            max_messages=defaults.max_messages,
            tools_config=config.tools,
            app_config=config,
            model_presets=preset_helpers.configured_model_presets(config),
            model_preset=defaults.model_preset,
            provider_snapshot_loader=provider_snapshot_loader,
            preset_snapshot_loader=preset_snapshot_loader,
            aux_providers=aux_providers,
            hooks=hooks or None,
            **extra,
        )

    def _sync_subagent_runtime_limits(self) -> None:
        """Keep subagent runtime limits aligned with mutable loop settings."""
        self.subagents.max_iterations = self.max_iterations

    def _apply_provider_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        publish_update: bool = True,
        model_preset: str | None = None,
    ) -> None:
        """Swap model/provider for future turns without disturbing an active one."""
        provider = snapshot.provider
        model = snapshot.model
        context_window_tokens = snapshot.context_window_tokens
        old_model = self.model
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.runner.provider = provider
        self.subagents.set_provider(provider, model)
        self.consolidator.set_provider(
            provider,
            model,
            context_window_tokens,
            preemptive_compact_ratio=snapshot.preemptive_compact_ratio,
        )
        self._provider_signature = snapshot.signature
        if publish_update and self._runtime_model_publisher is not None:
            self._runtime_model_publisher(
                self.model,
                model_preset if model_preset is not None else self.model_preset,
            )
        logger.info("Runtime model switched for next turn: {} -> {}", old_model, model)

    def _refresh_provider_snapshot(self) -> None:
        if self._provider_snapshot_loader is None:
            return
        try:
            snapshot = self._provider_snapshot_loader()
        except Exception:
            logger.exception("Failed to refresh provider config")
            return
        default_selection = preset_helpers.default_selection_signature(snapshot.signature)
        if self._active_preset and self._default_selection_signature in (None, default_selection):
            self._default_selection_signature = default_selection
            try:
                snapshot = self._build_model_preset_snapshot(self._active_preset)
            except Exception:
                logger.exception("Failed to refresh active model preset")
                return
        else:
            self._active_preset = None
            self._default_selection_signature = default_selection
        if snapshot.signature == self._provider_signature:
            return
        self._default_selection_signature = preset_helpers.default_selection_signature(snapshot.signature)
        self._apply_provider_snapshot(snapshot)

    @property
    def model_preset(self) -> str | None:
        return self._active_preset

    @model_preset.setter
    def model_preset(self, name: str | None) -> None:
        self.set_model_preset(name)

    def _build_model_preset_snapshot(self, name: str) -> ProviderSnapshot:
        return preset_helpers.build_runtime_preset_snapshot(
            name=name,
            presets=self.model_presets,
            provider=self.provider,
            loader=self._preset_snapshot_loader,
        )

    def set_model_preset(self, name: str | None, *, publish_update: bool = True) -> None:
        """Resolve a preset by name and apply all runtime model dependents."""
        name = preset_helpers.normalize_preset_name(name, self.model_presets)
        snapshot = self._build_model_preset_snapshot(name)
        self._apply_provider_snapshot(snapshot, publish_update=publish_update, model_preset=name)
        self._active_preset = name

    def _register_default_tools(self) -> None:
        """Register the default set of tools via plugin loader."""
        from durin.agent.tools.context import ToolContext
        from durin.agent.tools.loader import ToolLoader

        ctx = ToolContext(
            config=self.tools_config,
            workspace=str(self.workspace),
            bus=self.bus,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
            sessions=self.sessions,
            provider_snapshot_loader=self._provider_snapshot_loader,
            timezone=self.context.timezone or "UTC",
            aux_providers=self._aux_providers,
            app_config=self.app_config,
        )
        loader = ToolLoader()
        registered = loader.load(ctx, self.tools)

        # MyTool needs runtime state reference — manual registration
        if self.tools_config.my.enable:
            self.tools.register(
                MyTool(runtime_state=self, modify_allowed=self.tools_config.my.allow_set)
            )
            registered.append("my")

        logger.info("Registered {} tools: {}", len(registered), registered)

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from durin.agent.tools.mcp import connect_mcp_servers

        try:
            self._mcp_stacks = await connect_mcp_servers(self._mcp_servers, self.tools)
            if self._mcp_stacks:
                self._mcp_connected = True
            else:
                logger.warning("No MCP servers connected successfully (will retry next message)")
        except asyncio.CancelledError:
            logger.warning("MCP connection cancelled (will retry next message)")
            self._mcp_stacks.clear()
        except ImportError:
            res = ensure_or_note("mcp", config=getattr(self, "app_config", None))
            if res.status in ("present", "installed"):
                try:
                    self._mcp_stacks = await connect_mcp_servers(
                        self._mcp_servers, self.tools
                    )
                    if self._mcp_stacks:
                        self._mcp_connected = True
                except BaseException as e:  # noqa: BLE001
                    logger.warning("Failed to connect MCP after install: {}", e)
            else:
                logger.warning("MCP unavailable: {}", res.message)
        except BaseException as e:
            logger.warning("Failed to connect MCP servers (will retry next message): {}", e)
            self._mcp_stacks.clear()
        finally:
            self._mcp_connecting = False

    async def _warmup_memory_embedding(self) -> None:
        """Pre-load the embedding model in background so the first
        ``memory_store`` / ``memory_search`` doesn't pay ~18s download
        (first install) or ~230ms reload (subsequent boots) inline.

        Skipped silently when memory is disabled, fastembed isn't
        installed, or the model identifier is unknown — the existing
        tool-side fallback to grep keeps everything functional.
        """
        # Full config is optional — tests can construct AgentLoop without
        # it. No config → no warmup (tools still work via the grep path).
        if self.app_config is None:
            return
        try:
            if not getattr(self.app_config.memory, "enabled", False):
                return
        except AttributeError:
            return
        # Resilience: memory is enabled but the optional `[memory]` extra
        # (fastembed + lancedb) may be absent — a headless install, or one
        # where `durin onboard` was skipped. Don't degrade silently: warn
        # with the exact fix and fall back to grep-level recall. We do NOT
        # pip-install at runtime (the deliberate paths are `durin onboard`
        # and `durin doctor --install-missing`). Once the extra IS present,
        # the model weights auto-download on first use via fastembed, so no
        # separate fetch is needed here.
        from durin.memory.vector_index import vector_index_available

        if not vector_index_available():
            logger.warning(
                "Vector memory is enabled (memory.enabled=true) but the "
                "[memory] extra is not installed — falling back to grep-level "
                "recall. Install it with `durin doctor --install-missing` or "
                "`pip install 'durin-agent[memory]'`."
            )
            return
        model_name = self.app_config.memory.embedding.model
        try:
            from durin.memory.embedding import FastembedProvider
            await asyncio.to_thread(FastembedProvider.warmup, model_name)
            logger.info("Memory embedding model warmed: {}", model_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Memory embedding warmup skipped ({}): {}",
                model_name, exc,
            )

    def _set_tool_context(
        self, channel: str, chat_id: str,
        message_id: str | None = None, metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        from durin.agent.tools.context import ContextAware, RequestContext

        if session_key is not None:
            effective_key = session_key
        elif self._unified_session:
            effective_key = UNIFIED_SESSION_KEY
        else:
            effective_key = f"{channel}:{chat_id}"

        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=effective_key,
            metadata=dict(metadata or {}),
        )

        for name in self.tools.tool_names:
            tool = self.tools.get(name)
            if tool and isinstance(tool, ContextAware):
                tool.set_context(request_ctx)

    @staticmethod
    def _runtime_chat_id(msg: InboundMessage) -> str:
        """Return the chat id shown in runtime metadata for the model."""
        return str(msg.metadata.get("context_chat_id") or msg.chat_id)

    async def _build_bus_progress_callback(
        self, msg: InboundMessage
    ) -> Callable[..., Awaitable[None]]:
        """Build a progress callback that publishes to the message bus."""

        async def _bus_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_events: list[dict[str, Any]] | None = None,
            reasoning: bool = False,
            reasoning_end: bool = False,
            agent_ui: dict[str, Any] | None = None,
        ) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            if reasoning:
                meta["_reasoning_delta"] = True
            if reasoning_end:
                meta["_reasoning_end"] = True
            if tool_events:
                meta["_tool_events"] = tool_events
            if agent_ui is not None:
                meta[OUTBOUND_META_AGENT_UI] = agent_ui
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        return _bus_progress

    async def _build_retry_wait_callback(
        self, msg: InboundMessage
    ) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
        """Build a retry-wait callback that publishes to the message bus.

        The channel sees two flags: ``_retry_wait`` to identify the
        message kind, and ``retry_status`` carrying the structured
        payload (attempt / delay_s / kind / persistent / final) the UI
        needs to render the banner.
        """

        async def _on_retry_wait(content: str, status: dict[str, Any]) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            meta["retry_status"] = status
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        return _on_retry_wait

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        **kwargs: Any,
    ) -> bool:
        """Persist the triggering user message before the turn starts.

        Returns True if the message was persisted.
        """
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if has_text or media_paths:
            extra: dict[str, Any] = {"media": list(media_paths)} if media_paths else {}
            extra.update(kwargs)
            text = msg.content if isinstance(msg.content, str) else ""
            session.add_message("user", text, **extra)
            self._mark_pending_user_turn(session)
            self.sessions.save(session)
            return True
        return False

    def _build_initial_messages(
        self,
        msg: InboundMessage,
        session: Session,
        history: list[dict[str, Any]],
        pending_summary: str | None,
    ) -> list[dict[str, Any]]:
        """Build the initial message list for the LLM turn."""
        return self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=self._runtime_chat_id(msg),
            sender_id=msg.sender_id,
            session_summary=pending_summary,
            session_metadata=session.metadata,
            session_key=session.key,
            tools=self.tools.get_definitions() if self.tools else None,
            iteration=0,
        )

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Dispatch a command directly from the run() loop and publish the result."""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await t
        sub_cancelled = await self.subagents.cancel_by_session(key)
        return cancelled + sub_cancelled

    def _drop_active_task(self, task: asyncio.Task, key: str) -> None:
        """Done-callback: drop a finished task from its session's list.

        KeyError-safe (the key may already have been popped by
        ``_cancel_active_tasks``) and ValueError-safe (membership-checked
        before remove).
        """
        tasks = self._active_tasks.get(key)
        if tasks is not None and task in tasks:
            tasks.remove(task)

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    def _replay_token_budget(self) -> int:
        """Derive a token budget for session history replay from the context window."""
        if self.context_window_tokens <= 0:
            return 0
        max_output = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = self.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        *,
        session: Session | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool, list[dict[str, Any]]]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        Returns ``(final_content, tools_used, messages, stop_reason, had_injections, tool_events)``.
        The trailing ``tool_events`` is the runner's per-call event log
        (one entry per tool invocation, each with ``tool_call_id`` and
        ``duration_ms``). ``_save_turn`` uses it to record session-meta
        ``tool_call`` events with msg_index pointers.
        """
        self._sync_subagent_runtime_limits()

        loop_hook = AgentProgressHook(
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
            tool_hint_max_length=self.tool_hint_max_length,
            set_tool_context=self._set_tool_context,
            on_iteration=lambda iteration: self._on_iteration(iteration),
            on_cache_usage=lambda payload: setattr(self, "_last_cache_usage", payload),
        )
        hook: AgentHook = (
            CompositeHook([loop_hook] + self._extra_hooks) if self._extra_hooks else loop_hook
        )

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            When no messages are immediately available but sub-agents
            spawned in this dispatch are still running, blocks until at
            least one result arrives (or timeout).  This keeps the runner
            loop alive so subsequent sub-agent completions are consumed
            in-order rather than dispatched separately.
            """
            if pending_queue is None:
                return []

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = extract_documents(content, media)
                    media = media or None
                user_content = self.context._build_user_content(content, media)
                return {"role": "user", "content": user_content}

            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    items.append(_to_user_message(pending_queue.get_nowait()))
                except asyncio.QueueEmpty:
                    break

            # Block if nothing drained but sub-agents spawned in this dispatch
            # are still running.  Keeps the runner loop alive so subsequent
            # completions are injected in-order rather than dispatched separately.
            if (not items
                    and session is not None
                    and self.subagents.get_running_count_by_session(session.key) > 0):
                try:
                    msg = await asyncio.wait_for(pending_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timeout waiting for sub-agent completion in session {}",
                        session.key,
                    )
                    return items
                items.append(_to_user_message(msg))
                while len(items) < limit:
                    try:
                        items.append(_to_user_message(pending_queue.get_nowait()))
                    except asyncio.QueueEmpty:
                        break

            return items

        active_session_key = session.key if session else session_key
        file_state_token = bind_file_states(self._file_state_store.for_session(active_session_key))
        telemetry_token = None
        # A8: optional HTTPS push sink. Default OFF; opt-in via
        # cfg.telemetry.push.enabled. Captured here so the cleanup
        # path can call flush() before the process exits.
        push_sink_for_cleanup = None
        if active_session_key:
            try:
                from durin.telemetry.logger import bind_telemetry, get_session_logger
                session_logger = get_session_logger(active_session_key)
                try:
                    from durin.telemetry.wiring import wire_push_sink
                    push_sink_for_cleanup = wire_push_sink(
                        session_logger,
                        getattr(
                            getattr(self.app_config, "telemetry", None),
                            "push", None,
                        ),
                    )
                except Exception:  # noqa: BLE001
                    # Push wiring failure must NEVER affect the JSONL
                    # path. Log via the session_logger so the failure
                    # is itself recorded; the local sink keeps working.
                    push_sink_for_cleanup = None
                telemetry_token = bind_telemetry(session_logger)
            except Exception:
                telemetry_token = None
        # Sprint B / L3 — agent-mode provider, resolved per iteration so a
        # mid-run mode switch (via /plan or enter_plan_mode tool) takes
        # effect at the very next iteration. See docs/architecture/loop.md §3.
        def _mode_provider():
            from durin.agent.agent_mode import get_active_mode
            return get_active_mode(session)

        # OpenClaw-inspired compaction grace window. Exposed to the runner so
        # the outer LLM wall-clock timeout can extend its deadline once when
        # consolidation is in flight for this session — the slow call is
        # likely slow precisely because the consolidator hasn't finished
        # reshaping the context yet.
        _session_key_for_compact = session.key if session is not None else session_key
        def _is_compacting() -> bool:
            if not _session_key_for_compact:
                return False
            lock = self.consolidator.get_lock(_session_key_for_compact)
            return lock.locked()

        try:
            result = await self.runner.run(AgentRunSpec(
                initial_messages=initial_messages,
                tools=self.tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=hook,
                error_message="Sorry, I encountered an error calling the AI model.",
                concurrent_tools=True,
                workspace=self.workspace,
                session_key=session.key if session else None,
                context_window_tokens=self.context_window_tokens,
                context_block_limit=self.context_block_limit,
                provider_retry_mode=self.provider_retry_mode,
                progress_callback=on_progress,
                stream_progress_deltas=on_stream is not None,
                retry_wait_callback=on_retry_wait,
                checkpoint_callback=_checkpoint,
                injection_callback=_drain_pending,
                mode_provider=_mode_provider if session is not None else None,
                # Sustained goals may legitimately exceed DURIN_LLM_TIMEOUT_S; idle stall
                # is still capped by DURIN_STREAM_IDLE_TIMEOUT_S in streaming providers.
                llm_timeout_s=runner_wall_llm_timeout_s(
                    self.sessions,
                    session.key if session is not None else session_key,
                    metadata=(session.metadata if session is not None else None),
                ),
                is_compacting=_is_compacting,
                post_compaction_guard=self.consolidator.post_compaction_guard,
            ))
        finally:
            reset_file_states(file_state_token)
            # A8: drain the push sink BEFORE we let the logger go out
            # of scope. A partial buffer that's never drained loses
            # those events — flush is best-effort but worth the call.
            if push_sink_for_cleanup is not None:
                try:
                    push_sink_for_cleanup.flush()
                except Exception:  # noqa: BLE001
                    pass
            if telemetry_token is not None:
                from durin.telemetry.logger import reset_telemetry
                reset_telemetry(telemetry_token)
        self._last_usage = result.usage
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return (
            result.final_content,
            result.tools_used,
            result.messages,
            result.stop_reason,
            result.had_injections,
            list(result.tool_events or []),
        )

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        self._schedule_background(self._warmup_memory_embedding())
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            if self.commands.is_priority(raw):
                await self._dispatch_command_inline(
                    msg, msg.session_key, raw,
                    self.commands.dispatch_priority,
                )
                continue
            effective_key = self._effective_session_key(msg)
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self.commands.is_dispatchable_command(raw):
                    await self._dispatch_command_inline(
                        msg, effective_key, raw,
                        self.commands.dispatch,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self._pending_queues[effective_key].put_nowait(pending_msg)
                except asyncio.QueueFull:
                    logger.warning(
                        "Pending queue full for session {}, falling back to queued task",
                        effective_key,
                    )
                else:
                    logger.info(
                        "Routed follow-up message to pending queue for session {}",
                        effective_key,
                    )
                    continue
            # Register the pending queue synchronously, *before* create_task,
            # so a same-session message consumed before the dispatch task
            # starts is routed here (mid-turn injection) instead of spawning a
            # competing task (B3). `create_task` only schedules the coroutine —
            # it does not run it — so registering inside `_dispatch` left a
            # window the consumer could re-enter for the same session.
            pending: asyncio.Queue = asyncio.Queue(maxsize=20)
            self._pending_queues[effective_key] = pending
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            task = asyncio.create_task(self._dispatch(msg, pending))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._drop_active_task(t, k)
            )

    async def _dispatch(
        self, msg: InboundMessage, pending: asyncio.Queue | None = None,
    ) -> None:
        """Process a message: per-session serial, cross-session concurrent.

        ``pending`` is the mid-turn injection queue the consumer registers for
        this session (under the same effective key) *before* spawning this
        task, so a follow-up is visible the instant it arrives (B3). When
        called directly without it (a unit test, or any non-consumer caller)
        the task self-provisions and registers one. Either way this task owns
        the queue's lifecycle — it drains and unregisters it in the ``finally``.
        """
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()

        if pending is None:
            pending = asyncio.Queue(maxsize=20)
            self._pending_queues[session_key] = pending

        try:
            async with lock, gate:
                try:
                    on_stream = on_stream_end = None
                    if msg.metadata.get("_wants_stream"):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content=delta,
                                metadata=meta,
                            ))

                        async def on_stream_end(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="",
                                metadata=meta,
                            ))
                            stream_segment += 1

                    response = await self._process_message(
                        msg, on_stream=on_stream, on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    if response is not None:
                        await self.bus.publish_outbound(response)
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata=msg.metadata or {},
                        ))
                    if msg.channel == "websocket":
                        # Signal that the turn is fully complete (all tools executed,
                        # final text streamed).  This lets WS clients know when to
                        # definitively stop the loading indicator.
                        turn_lat = self._pending_turn_latency_ms.pop(session_key, None)
                        turn_metadata: dict[str, Any] = {**msg.metadata, "_turn_end": True}
                        if turn_lat is not None:
                            turn_metadata["latency_ms"] = int(turn_lat)
                        sess_turn = self.sessions.get_or_create(session_key)
                        turn_metadata["goal_state"] = goal_state_ws_blob(sess_turn.metadata)
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata=turn_metadata,
                        ))
                        if msg.metadata.get("webui") is True:
                            async def _generate_title_and_notify() -> None:
                                generated = await maybe_generate_webui_title_after_turn(
                                    channel=msg.channel,
                                    metadata=msg.metadata,
                                    sessions=self.sessions,
                                    session_key=session_key,
                                    provider=self.provider,
                                    model=self.model,
                                )
                                if generated:
                                    await self.bus.publish_outbound(OutboundMessage(
                                        channel=msg.channel,
                                        chat_id=msg.chat_id,
                                        content="",
                                        metadata={**msg.metadata, "_session_updated": True},
                                    ))

                            self._schedule_background(_generate_title_and_notify())
                except asyncio.CancelledError:
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    try:
                        key = self._effective_session_key(msg)
                        session = self.sessions.get_or_create(key)
                        if self._restore_runtime_checkpoint(session):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    raise
                except Exception:
                    logger.exception("Error processing message for session {}", session_key)
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    ))
        finally:
            # Drain any messages still in the pending queue and re-publish
            # them to the bus so they are processed as fresh inbound messages
            # rather than silently lost.
            queue = self._pending_queues.pop(session_key, None)
            if queue is not None:
                leftover = 0
                while True:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self.bus.publish_inbound(item)
                    leftover += 1
                if leftover:
                    logger.info(
                        "Re-published {} leftover message(s) to bus for session {}",
                        leftover, session_key,
                    )
            await publish_turn_run_status(self.bus, msg, "idle")
            self._pending_turn_latency_ms.pop(session_key, None)

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        # Kill orphan background process groups (exec background=true).
        # Use the module global directly so shutdown never instantiates a
        # registry that was never used.
        from durin.agent.tools import process_registry as _proc_reg
        if _proc_reg._registry is not None:
            with suppress(Exception):
                await _proc_reg._registry.shutdown()
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        # A11: drain memory background services so the watchdog
        # Observer and health-check thread terminate cleanly.
        self._stop_memory_background_services()
        logger.info("Agent loop stopping")

    async def _process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a system inbound message (e.g. subagent announce)."""
        channel, chat_id = (
            msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        )
        logger.info("Processing system message from {}", msg.sender_id)
        key = msg.session_key_override or f"{channel}:{chat_id}"
        session = self.sessions.get_or_create(key)
        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)

        pending = self._format_pending_summary(session)
        if pending:
            logger.info("Memory compact triggered for session {}", key)

        await self.consolidator.maybe_consolidate_by_tokens(
            session,
            replay_max_messages=self._max_messages,
        )
        is_subagent = msg.sender_id == "subagent"
        if is_subagent and self._persist_subagent_followup(session, msg):
            logger.debug("Subagent result persisted for session {}", key)
            self.sessions.save(session)
        self._set_tool_context(
            channel, chat_id, msg.metadata.get("message_id"),
            msg.metadata, session_key=key,
        )
        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        history = session.get_history(**_hist_kwargs)
        current_role = "assistant" if is_subagent else "user"

        messages = self.context.build_messages(
            history=history,
            current_message="" if is_subagent else msg.content,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            sender_id=msg.sender_id,
            session_summary=pending,
            session_metadata=session.metadata,
            session_key=key,
            tools=self.tools.get_definitions() if self.tools else None,
            iteration=0,
        )
        t_wall = time.time()
        final_content, _, all_msgs, stop_reason, _, tool_events = await self._run_agent_loop(
            messages, session=session, channel=channel, chat_id=chat_id,
            message_id=msg.metadata.get("message_id"),
            metadata=msg.metadata,
            session_key=key,
            pending_queue=pending_queue,
        )
        wall_done = time.time()
        latency_ms = max(0, int((wall_done - t_wall) * 1000))
        self._save_turn(
            session, all_msgs, 1 + len(history),
            turn_latency_ms=latency_ms,
            tool_events=tool_events,
        )
        if channel == "websocket":
            self._pending_turn_latency_ms[key] = latency_ms
        session.enforce_file_cap(on_archive=self.context.memory.raw_archive)
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(
                session,
                replay_max_messages=self._max_messages,
            )
        )
        content = final_content or "Background task completed."
        outbound_metadata: dict[str, Any] = {}
        if channel == "slack" and key.startswith("slack:") and key.count(":") >= 2:
            outbound_metadata["slack"] = {"thread_ts": key.split(":", 2)[2]}
        if origin_message_id := msg.metadata.get("origin_message_id"):
            outbound_metadata["origin_message_id"] = origin_message_id
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata=outbound_metadata,
        )

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        self._refresh_provider_snapshot()

        if msg.channel == "system":
            return await self._process_system_message(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                pending_queue=pending_queue,
            )

        key = session_key or msg.session_key
        ctx = TurnContext(
            msg=msg,
            session=None,
            session_key=key,
            state=TurnState.RESTORE,
            turn_id=f"{key}:{time.time_ns()}",
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
        )

        while ctx.state is not TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                raise RuntimeError(f"Missing state handler for {ctx.state}")

            t0 = time.perf_counter()
            try:
                event = await handler(ctx)
            except Exception:
                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(
                    StateTraceEntry(
                        state=ctx.state,
                        started_at=t0,
                        duration_ms=duration,
                        event="",
                        error="exception",
                    )
                )
                raise

            duration = (time.perf_counter() - t0) * 1000
            ctx.trace.append(
                StateTraceEntry(
                    state=ctx.state,
                    started_at=t0,
                    duration_ms=duration,
                    event=event,
                )
            )
            logger.debug(
                "[turn {}] State {} took {:.1f}ms -> event {}",
                ctx.turn_id,
                ctx.state.name,
                duration,
                event,
            )

            next_state = self._TRANSITIONS.get((ctx.state, event))
            if next_state is None:
                raise RuntimeError(
                    f"[turn {ctx.turn_id}] No transition from {ctx.state} "
                    f"on event {event!r}"
                )
            ctx.state = next_state

        logger.debug(
            "[turn {}] Turn completed after {} states",
            ctx.turn_id,
            len(ctx.trace),
        )
        return ctx.outbound

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        all_msgs: list[dict[str, Any]],
        stop_reason: str,
        had_injections: bool,
        on_stream: Callable[[str], Awaitable[None]] | None,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """Assemble the final outbound message from turn results."""
        # MessageTool suppression
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            if not had_injections or stop_reason == "empty_final_response":
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason not in {"error", "tool_error"}:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    async def _state_restore(self, ctx: TurnContext) -> TurnState:
        """Restore checkpoint / pending user turn; extract documents."""
        msg = ctx.msg

        if msg.media:
            new_content, image_only = extract_documents(msg.content, msg.media)
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_only)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # Session is already fetched by the caller (_process_message) but
        # ensure it exists in case this handler is invoked independently.
        if ctx.session is None:
            ctx.session = self.sessions.get_or_create(ctx.session_key)
        mark_webui_session(ctx.session, msg.metadata)

        if self._restore_runtime_checkpoint(ctx.session):
            self.sessions.save(ctx.session)
        if self._restore_pending_user_turn(ctx.session):
            self.sessions.save(ctx.session)

        return "ok"

    async def _state_compact(self, ctx: TurnContext) -> str:
        ctx.pending_summary = self._format_pending_summary(ctx.session)
        return "ok"

    async def _state_command(self, ctx: TurnContext) -> str:
        raw = ctx.msg.content.strip()
        cmd_ctx = CommandContext(
            msg=ctx.msg, session=ctx.session, key=ctx.session_key, raw=raw, loop=self
        )
        result = await self.commands.dispatch(cmd_ctx)
        if result is not None:
            ctx.outbound = result
            # Shortcut commands skip BUILD and SAVE, so we must persist the
            # turn here so WebUI history hydration after _turn_end sees the
            # message.  Mark messages with _command so get_history can filter
            # them out of LLM context.  /new is excluded because it
            # intentionally clears the session.
            if raw.lower() != "/new":
                ctx.user_persisted_early = self._persist_user_message_early(
                    ctx.msg, ctx.session, _command=True
                )
                ctx.session.add_message(
                    "assistant", result.content, _command=True
                )
                self.sessions.save(ctx.session)
                self._clear_pending_user_turn(ctx.session)
            return "shortcut"
        return "dispatch"

    async def _state_build(self, ctx: TurnContext) -> str:
        await self.consolidator.maybe_consolidate_by_tokens(
            ctx.session,
            replay_max_messages=self._max_messages,
        )
        self._set_tool_context(
            ctx.msg.channel,
            ctx.msg.chat_id,
            ctx.msg.metadata.get("message_id"),
            ctx.msg.metadata,
            session_key=ctx.session_key,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        ctx.history = ctx.session.get_history(**_hist_kwargs)

        ctx.initial_messages = self._build_initial_messages(
            ctx.msg, ctx.session, ctx.history, ctx.pending_summary
        )
        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session
        )

        if ctx.on_progress is None:
            ctx.on_progress = await self._build_bus_progress_callback(ctx.msg)
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = await self._build_retry_wait_callback(ctx.msg)

        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        await publish_turn_run_status(self.bus, ctx.msg, "running")
        result = await self._run_agent_loop(
            ctx.initial_messages,
            on_progress=ctx.on_progress,
            on_stream=ctx.on_stream,
            on_stream_end=ctx.on_stream_end,
            on_retry_wait=ctx.on_retry_wait,
            session=ctx.session,
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            message_id=ctx.msg.metadata.get("message_id"),
            metadata=ctx.msg.metadata,
            session_key=ctx.session_key,
            pending_queue=ctx.pending_queue,
        )
        final_content, tools_used, all_msgs, stop_reason, had_injections, tool_events = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        ctx.tool_events = tool_events
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        if ctx.final_content is None or not ctx.final_content.strip():
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        ctx.save_skip = 1 + len(ctx.history) + (1 if ctx.user_persisted_early else 0)
        mt = self.tools.get("message")
        extra = getattr(mt, "turn_delivered_media_paths", lambda: [])() if mt else []
        merge_turn_media_into_last_assistant(ctx.all_messages, extra)
        # Stop re-injecting the executing-plan pointer once the plan's todos
        # are all completed (the cursor reached the end), so it doesn't linger
        # into unrelated turns.
        from durin.agent.agent_mode import clear_executing_plan_if_todos_done
        clear_executing_plan_if_todos_done(ctx.session.metadata)

        # E2 Part A: durable skill-usage signal. Scan only THIS turn's new
        # messages (the same slice _save_turn persists) so calls aren't
        # re-counted every turn as the conversation accumulates.
        _new_messages = ctx.all_messages[ctx.save_skip:]
        _skill_calls = extract_skill_calls(_new_messages)
        if _skill_calls:
            ctx.session.metadata.setdefault("skill_calls", []).extend(_skill_calls)

        ctx.turn_latency_ms = max(0, int((time.time() - ctx.turn_wall_started_at) * 1000))
        self._save_turn(
            ctx.session, ctx.all_messages, ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
            tool_events=ctx.tool_events,
        )
        if ctx.msg.channel == "websocket":
            self._pending_turn_latency_ms[ctx.session_key] = ctx.turn_latency_ms
        ctx.session.enforce_file_cap(on_archive=self.context.memory.raw_archive)
        self._clear_pending_user_turn(ctx.session)
        self._clear_runtime_checkpoint(ctx.session)
        self.sessions.save(ctx.session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(
                ctx.session,
                replay_max_messages=self._max_messages,
            )
        )
        return "ok"

    async def _state_respond(self, ctx: TurnContext) -> str:
        ctx.outbound = self._assemble_outbound(
            ctx.msg,
            ctx.final_content,
            ctx.all_messages,
            ctx.stop_reason,
            ctx.had_injections,
            ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        return "ok"

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
        tool_events: list[dict[str, Any]] | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results.

        When ``tool_events`` is provided, also writes one ``type=tool_call``
        event to the per-session meta file for each tool call emitted by
        an assistant message that gets persisted. ``msg_index`` points to
        the position of that assistant message in ``session.messages``
        after this turn's append; parallel tool calls under one assistant
        share the same ``msg_index`` but have distinct ``id`` (the LLM's
        ``tool_call_id``).
        """
        from datetime import datetime

        # Index tool events by tool_call_id so we can correlate them with
        # the tool_calls embedded in each assistant message we persist.
        events_by_id: dict[str, dict[str, Any]] = {}
        for ev in tool_events or []:
            if not isinstance(ev, dict):
                continue
            tc_id = ev.get("tool_call_id")
            if isinstance(tc_id, str) and tc_id:
                events_by_id[tc_id] = ev

        meta_events_to_write: list[dict[str, Any]] = []

        last_assistant_idx: int | None = None
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                tool_name = entry.get("name")
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = _truncate_tool_output(
                        content, self.max_tool_result_chars, tool_name,
                    )
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, should_truncate_text=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and ContextBuilder._RUNTIME_CONTEXT_TAG in content:
                    # Strip the runtime-context block appended at the end.
                    tag_pos = content.find(ContextBuilder._RUNTIME_CONTEXT_TAG)
                    before = content[:tag_pos].rstrip("\n ")
                    if before:
                        entry["content"] = before
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            assistant_idx = len(session.messages) - 1
            if role == "assistant":
                last_assistant_idx = assistant_idx
                # Collect meta events for any tool_calls this message emitted.
                tool_calls = entry.get("tool_calls") or []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id")
                    if not isinstance(tc_id, str):
                        continue
                    ev = events_by_id.get(tc_id)
                    if ev is None:
                        continue
                    name = (
                        (tc.get("function") or {}).get("name")
                        if isinstance(tc.get("function"), dict)
                        else None
                    ) or ev.get("name") or "unknown"
                    outcome = "error" if ev.get("status") == "error" else "ok"
                    from durin.session.session_meta import make_tool_call_event

                    meta_events_to_write.append(make_tool_call_event(
                        tool_call_id=tc_id,
                        name=str(name),
                        outcome=outcome,
                        msg_index=assistant_idx,
                        duration_ms=float(ev.get("duration_ms") or 0.0),
                        error=ev.get("detail") if outcome == "error" else None,
                    ))
        if turn_latency_ms is not None and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["latency_ms"] = int(turn_latency_ms)
        session.updated_at = datetime.now()

        # Best-effort meta write — never break the agent if the meta file
        # is unavailable. The session itself remains the source of truth.
        if meta_events_to_write and session.key:
            with suppress(Exception):
                from durin.session.session_meta import append_events_batch, meta_path_for

                meta_path = meta_path_for(session.key, self.sessions.sessions_dir)
                append_events_batch(meta_path, session.key, meta_events_to_write)

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        task_id = msg.metadata.get("subagent_task_id") if isinstance(msg.metadata, dict) else None
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
        )
        return True

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _format_pending_summary(self, session: Session) -> str | None:
        """Read the consolidator's last summary and wrap it with an
        archive marker so the next turn can distinguish "this is a
        summary" from "this is real conversation".

        Returns ``None`` when no summary has been persisted yet (fresh
        session or no consolidation rounds have run).

        A10 (2026-05-28): the summary primarily lives at
        ``memory/session_summary/<sanitized_key>.md`` (single source
        of truth). For backward compatibility with pre-A10 sessions
        whose metadata still carries the legacy ``_last_summary``
        dict, we fall through to that path when the markdown isn't
        there yet (the next compaction migrates it).
        """
        from durin.memory.session_summary_store import get_session_summary

        text: str | None = None
        last_active: str | None = None

        md_text, md_last_active = get_session_summary(self.workspace, session.key)
        if md_text:
            text = md_text
            last_active = md_last_active.isoformat() if md_last_active else None
        else:
            # Legacy fallback: pre-A10 metadata. Gets migrated next compaction.
            meta = session.metadata.get("_last_summary")
            if isinstance(meta, dict):
                cand = meta.get("text")
                if isinstance(cand, str) and cand:
                    text = cand
                    raw_last = meta.get("last_active")
                    last_active = raw_last if isinstance(raw_last, str) else None

        if not text:
            return None
        header = "consolidator"
        if last_active:
            header = f"consolidator, last active {last_active}"
        return (
            f"=== ARCHIVED SUMMARY ({header}) ===\n"
            f"{text}\n"
            f"=== END ARCHIVED SUMMARY ==="
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        return True

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id,
            content=content, media=media or [],
        )
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )
