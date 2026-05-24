"""Runtime context for tool construction."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from durin.providers.base import LLMProvider


@dataclass(frozen=True)
class RequestContext:
    """Per-request context injected into tools at message-processing time."""
    channel: str
    chat_id: str
    message_id: str | None = None
    session_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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
    sessions: Any | None = None
    file_state_store: Any = field(default=None)
    provider_snapshot_loader: Callable[[], Any] | None = None
    image_generation_provider_configs: dict[str, Any] | None = None
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
