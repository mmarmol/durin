"""Runtime context for tool construction."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from durin.providers.base import LLMProvider


# The per-result character cap of the agent run that is calling a tool. The
# runner takes anything larger out of the model's context and leaves a short
# preview, which drops a tool's own "continue from here" footer. Tools that
# page their own output (read_file, grep) size each page under this cap so a
# page arrives whole. Unset outside an agent run (direct calls, tests): those
# callers apply no such cap, so tools keep their own limits.
_RESULT_CHAR_CAP: ContextVar[int | None] = ContextVar("result_char_cap", default=None)


def set_result_char_cap(cap: int | None) -> Token:
    """Publish the calling run's per-result cap; returns the reset token."""
    return _RESULT_CHAR_CAP.set(cap if cap and cap > 0 else None)


def reset_result_char_cap(token: Token) -> None:
    _RESULT_CHAR_CAP.reset(token)


def current_result_char_cap() -> int | None:
    """The calling run's per-result cap, or None outside an agent run."""
    return _RESULT_CHAR_CAP.get()


@dataclass(frozen=True)
class RequestContext:
    """Per-request context injected into tools at message-processing time."""
    channel: str
    chat_id: str
    message_id: str | None = None
    session_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class RequestContextVar:
    """Holds the current turn's :class:`RequestContext` for one tool instance.

    One tool instance serves every turn running at the same time: chats run
    concurrently, and cron and API turns share the same tools. A plain
    attribute would hold whichever turn set it last, not the turn now calling
    the tool. A ContextVar gives each asyncio task its own value: a turn sets
    it before its tools run, and the tasks that run those tools inherit it.
    """

    def __init__(self, name: str = "request_ctx") -> None:
        self._var: ContextVar[RequestContext | None] = ContextVar(name, default=None)

    def set(self, ctx: RequestContext | None) -> None:
        self._var.set(ctx)

    def get(self) -> RequestContext | None:
        return self._var.get()


@runtime_checkable
class ContextAware(Protocol):
    def set_context(self, ctx: RequestContext) -> None:
        ...


@dataclass(frozen=True, slots=True)
class AuxProviderHandle:
    """Bound provider + model name for an auxiliary modality bridge.

    Constructed once at startup from ``config.agents.aux_models`` and
    handed to bridge tools (``interpret_image``, ``interpret_audio``,
    …) via :class:`ToolContext`. The bridge tool reuses the same
    provider instance for every call so we don't pay credentials /
    client-setup cost per request.
    """

    provider: "LLMProvider"
    model: str


@dataclass
class ToolContext:
    config: Any
    workspace: str
    bus: Any | None = None
    subagent_manager: Any | None = None
    cron_service: Any | None = None
    automations_runtime: Any | None = None
    sessions: Any | None = None
    file_state_store: Any = field(default=None)
    provider_snapshot_loader: Callable[[], Any] | None = None
    timezone: str = "UTC"
    # Auxiliary providers for capability bridges (vision / audio / …).
    # Populated when ``config.agents.aux_models`` has the corresponding
    # entry. Bridge tools check the relevant key in ``enabled()`` so
    # they only appear in the LLM's tool list when a bridge target is
    # actually available.
    aux_providers: dict[str, AuxProviderHandle] = field(default_factory=dict)
    # Full :class:`DurinConfig` for tools that need cross-section access
    # (e.g. ``memory_search`` reading ``memory.enabled``+ ``memory.embedding``
    # while ``config`` only carries ``cfg.tools``). Optional so ad-hoc
    # test constructions don't have to thread the whole config — tools
    # treat ``None`` the same as a missing section and fall back to grep
    # / disabled behaviour.
    app_config: Any | None = None
    # The live tool registry (the loop's own ``self.tools``), passed by reference
    # so tools that compose sub-runs (e.g. ``run_workflow``) can reuse already-
    # connected MCP tools without reconnecting. Populated only for the core loop;
    # ``None`` elsewhere.
    live_tool_registry: Any | None = None
    # Which prompt profile the calling agent runs: "core" (the main
    # loop's full stable tier, pinned memory + hot layer included) or
    # "subagent" (focused prompt WITHOUT the memory prefix — see
    # ``SubagentManager._build_subagent_prompt``). Tools that reason
    # about what the caller already sees in its system prompt (e.g.
    # ``memory_search`` hot-layer dedup) must key off this.
    scope: str = "core"
    # The gateway's live MCP handle (``McpRuntime``, wrapping this same
    # ``AgentLoop``) — the same object the REST service registry is built
    # with (see ``cli/commands.py``'s unified-gateway wiring). Populated by
    # ``AgentLoop._register_default_tools`` so ``McpManageTool.create`` can
    # hand its ``McpService`` a live runtime instead of a config-only one;
    # without it, ``McpService.approved_config``/``mark_approved`` are
    # inert (no runtime to track against) and an agent reconnect can never
    # succeed, even for an entirely unchanged boot config. ``None`` outside
    # the main loop (TUI, contract generation, tests that build a bare
    # ``ToolContext``).
    mcp_runtime: Any | None = None
    # The folder a relative path resolves in for the tools that take paths, in
    # place of the per-session work area the chat derives from the request
    # context: a workflow node's working folder. A path under a managed
    # top-level name (``workflows/``, ``memory/``, ...) still resolves from the
    # workspace root. None: the session work area, else the workspace root.
    work_dir: str | None = None
