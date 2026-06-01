"""Configuration schema using Pydantic."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from durin.cron.types import CronSchedule

if TYPE_CHECKING:
    from durin.agent.tools.self import MyToolConfig
    from durin.agent.tools.shell import ExecToolConfig
    from durin.agent.tools.web import WebToolsConfig


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChannelsConfig(Base):
    """Configuration for chat channels.

    Built-in and plugin channel configs are stored as extra fields (dicts).
    Each channel parses its own config in __init__.
    Per-channel "streaming": true enables streaming output (requires send_delta impl).
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
    show_reasoning: bool = True  # surface model reasoning when channel implements it
    send_max_retries: int = Field(default=3, ge=0, le=10)  # Max delivery attempts (initial send included)
    transcription_provider: str = "groq"  # Voice transcription backend: "groq" or "openai"
    transcription_language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")  # Optional ISO-639-1 hint for audio transcription


class DreamConfig(Base):
    """Dream memory consolidation configuration."""

    _HOUR_MS = 3_600_000

    interval_h: int = Field(default=2, ge=1)  # Every 2 hours by default
    cron: str | None = Field(default=None, exclude=True)  # Legacy compatibility override
    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model", "model_override"),
    )  # Optional Dream-specific model override
    max_batch_size: int = Field(default=20, ge=1)  # Max history entries per run
    # Bumped from 10 to 15 in #3212 (exp002: +30% dedup, no accuracy loss; >15 plateaus).
    max_iterations: int = Field(default=15, ge=1)  # Max tool calls per Phase 2
    # Per-line git-blame age annotation in Phase 1 prompt (see #3212). Default
    # on — set to False to feed MEMORY.md raw if a specific LLM reacts poorly
    # to the `← Nd` suffix or you want deterministic, git-independent prompts.
    annotate_line_ages: bool = True
    # Pre-LLM gate (2026-05-31): when the cron fires, tokenize the unprocessed
    # history.jsonl tail and skip the Phase 1 LLM call entirely if total tokens
    # are below this threshold. Cheap (a few ms with tiktoken) and bounds the
    # cost of a cron that ticks during quiet periods — a few "ok" / social
    # turns no longer trigger a multi-thousand-token analysis. Default 2000:
    # filters trivial / social sessions, keeps anything substantive enough to
    # produce a fact worth escalating to MEMORY.md / SOUL.md / skills.
    # Set to 0 to disable the gate (every non-empty cron tick runs the LLM,
    # the pre-2026-05-31 behaviour).
    min_tokens_to_run: int = Field(
        default=2000, ge=0,
        validation_alias=AliasChoices("minTokensToRun", "min_tokens_to_run"),
    )

    def build_schedule(self, timezone: str) -> CronSchedule:
        """Build the runtime schedule, preferring the legacy cron override if present."""
        if self.cron:
            return CronSchedule(kind="cron", expr=self.cron, tz=timezone)
        return CronSchedule(kind="every", every_ms=self.interval_h * self._HOUR_MS)

    def describe_schedule(self) -> str:
        """Return a human-readable summary for logs and startup output."""
        if self.cron:
            return f"cron {self.cron} (legacy)"
        hours = self.interval_h
        return f"every {hours}h"


class InlineFallbackConfig(Base):
    """One inline fallback model configuration."""

    model: str
    provider: str
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None


FallbackCandidate = str | InlineFallbackConfig


class AuxModelConfig(Base):
    """Inline model handle for an auxiliary bridge (vision, audio, pdf, …).

    Either ``preset`` (referencing a named ``model_presets`` entry) or
    the inline ``model`` + ``provider`` pair must be supplied. The
    bridge tools resolve this at call time, so swapping the aux model
    takes effect immediately without a restart.
    """

    preset: str | None = None
    model: str | None = None
    provider: str = "auto"


class MemoryEmbeddingConfig(Base):
    """Embedding model configuration for the memory subsystem (Phase 2).

    ``provider`` selects the adapter (currently only ``fastembed``;
    future: ``openai``, ``ollama``). ``model`` must exist in fastembed's
    catalog for the installed version (validated at
    ``FastembedProvider`` construction time). ``base_url`` and
    ``api_key`` are passed through to HTTP providers when added —
    fastembed ignores them.

    ``lazy_eviction`` is reserved: V1 keeps the model resident for the
    life of the process and emits load/embed telemetry so eviction can
    be turned on later if the data warrants it.
    """

    provider: str = "fastembed"
    # Default model: multilingual-e5-small (registered as custom model
    # in durin/memory/embedding.py::_CUSTOM_MODELS). 117M params, 384-
    # dim, 100+ languages, MIT, retrieval-tuned. Replaced
    # paraphrase-multilingual-MiniLM-L12-v2 on 2026-05-30 — see doc 02
    # §indexing and the wizard `_EMBEDDING_CHOICES` for the rationale.
    model: str = "intfloat/multilingual-e5-small"
    base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("baseUrl", "base_url"),
    )
    api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("apiKey", "api_key"),
    )
    lazy_eviction: bool = Field(
        default=False,
        validation_alias=AliasChoices("lazyEviction", "lazy_eviction"),
    )


class MemoryDreamConfig(Base):
    """Entity-centric dream auto-trigger config (doc 25 §2.A.1).

    Distinct from ``agents.defaults.dream`` which schedules the legacy
    ``MEMORY.md`` / ``SOUL.md`` consolidator. This block governs when
    the entity-centric :class:`DreamConsolidator` runs automatically
    over pending post-cursor entries.

    Four triggers (any combination):

    - **cron**: daily schedule (predictable, OpenClaw-style).
    - **post_compaction**: dream after the conversation consolidator
      compacts a session — the context is already in memory so the
      cost is amortised.
    - **on_session_close**: dream when a session ends (``/quit`` or
      idle timeout).
    - **threshold_entries**: dream when an entity accumulates this
      many post-cursor entries (per-entity granularity).

    ``min_seconds_between_runs`` throttles the per-entity triggers so
    fast-firing events (e.g. a flurry of memory_store calls) don't
    cause thrashing.
    """

    # Master switch — false disables all four triggers; manual
    # ``durin memory dream`` still works.
    enabled: bool = True

    # Cron expression for the daily pass. 3am local to avoid the
    # legacy ``dream`` job's every-2h schedule.
    cron: str = "0 3 * * *"

    # Per-entity threshold. 0 disables the threshold trigger.
    threshold_entries: int = Field(default=5, ge=0)

    # Hook into the session compaction lifecycle.
    post_compaction: bool = True

    # Hook into session-close events.
    on_session_close: bool = True

    # Override the dream model (None → falls through to
    # agents.defaults.model used by ``durin memory dream``).
    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model_override"),
    )

    # Cooldown between auto-runs per workspace to prevent thrashing
    # when multiple triggers fire fast.
    min_seconds_between_runs: int = Field(
        default=300,
        ge=0,
        validation_alias=AliasChoices("minSecondsBetweenRuns", "min_seconds_between_runs"),
    )

    # Hard wall-clock cap per dream pass. The runner drains an entity's
    # backlog across multiple consolidate batches (FIFO oldest-first,
    # cursor advances per batch); this caps how long that drain runs
    # before yielding. Remainder fires `memory.dream.budget_exhausted`
    # telemetry and is picked up by the next trigger. Default 600s
    # (10 min) covers ~10 oldest-first batches per entity at glm-5.1
    # speeds; bump up for bootstraps of large entities, down for
    # tighter cron windows.
    max_seconds_per_run: int = Field(
        default=600,
        ge=0,
        validation_alias=AliasChoices("maxSecondsPerRun", "max_seconds_per_run"),
    )

    # §2.D auto-absorb config nested under dream.
    auto_absorb: "AutoAbsorbConfig" = Field(
        default_factory=lambda: AutoAbsorbConfig(),
        validation_alias=AliasChoices("autoAbsorb", "auto_absorb"),
    )


class AutoAbsorbConfig(Base):
    """Auto-absorb post-dream config (doc 25 §2.D).

    After a successful dream pass, optionally run an LLM-judge over
    alias-overlap candidates and auto-merge those above the confidence
    threshold. Designed to close the loop between dream consolidation
    and manual ``durin memory absorb`` without destructive false-merges
    (see archived doc 24 §7 for the risk analysis).

    Defaults are opt-in conservative: disabled by default, threshold
    high enough that only obvious matches pass, quarantine window so
    a dream pass that just created two entities can't re-judge them
    on the same run.

    The merge itself reuses :meth:`EntityAbsorption.absorb` (which
    preserves content from both pages via ``_merge_pages``, archives
    the absorbed page under ``entities/<type>/<canonical>/archive/``,
    and records the action in a git commit with full reasoning in the
    trailers). Recovery: ``cd memory && git revert <sha>``.
    """

    # Master switch — keep OFF by default; the blast radius of a
    # silent bad merge is high enough that opt-in is the right
    # ergonomics.
    enabled: bool = False

    # LLM-judge confidence floor (0-100). Default 95 favours
    # precision over recall: most pairs that warrant a merge will
    # also warrant manual review at this threshold. Tune down with
    # data from ``memory.absorb.judged`` telemetry.
    confidence_threshold: int = Field(
        default=95,
        ge=0,
        le=100,
        validation_alias=AliasChoices("confidenceThreshold", "confidence_threshold"),
    )

    # Quarantine: a candidate is only judged if BOTH pages were
    # created (or last dreamed) at least this many hours ago. Blocks
    # the "premature consolidation" loop where a dream pass that
    # alucinated two near-identical pages immediately merges its own
    # output (glm peer review C3, 2026-05-24).
    min_age_hours: int = Field(
        default=24,
        ge=0,
        validation_alias=AliasChoices("minAgeHours", "min_age_hours"),
    )

    # Override the judge model (None → use the dream model, which is
    # the runner's ``model`` field). Setting a different model
    # mitigates the self-consistency bias where the same model
    # judges its own output.
    judge_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("judgeModel", "judge_model"),
    )


class CrossEncoderConfig(Base):
    """Cross-encoder reranker config (doc 03 §9, doc 10 P4).

    OFF by default per spec — the model adds 300-1500ms latency and
    requires ~1GB additional RAM. Power users with quality-sensitive
    workloads enable it; the default pipeline already produces useful
    retrieval.
    """

    enabled: bool = False
    # Default model: BAAI/bge-reranker-base (MIT, ~100M params,
    # multilingual). Switched from jinaai/jina-reranker-v2-base-
    # multilingual on 2026-05-30 — see H30 note in
    # `durin/memory/cross_encoder.py`. Override to one of the curated
    # alternatives (bge-v2-m3 for heavy multilingual, others) if you
    # want a different trade-off.
    model: str = "BAAI/bge-reranker-base"
    batch_size: int = 32
    # Top-N to keep after the rerank step. Doc 03 §9.3 says 10.
    top_n: int = 10


class MemorySearchSectioningConfig(Base):
    """Sectioning step configuration (audit G1, 2026-05-28).

    ``max_per_source`` caps how many `corpus` hits sharing the same
    `ingest_id` can survive the sectioning step. Default 3 — set
    when an ingested document is chunked into many corpus entries
    and a single semantic query would otherwise monopolise the
    top-K with consecutive chunks of the same source. Doc 03 §12.4.

    Pre-G1 the value was hard-coded at
    `durin.memory.sectioned_output.DEFAULT_MAX_PER_SOURCE`. Doc 03
    §16 row 8 had promised configurability since Phase 3 but the
    field never landed. G1 ships it; default unchanged so existing
    workspaces see zero behaviour change.
    """

    max_per_source: int = 3


class MemorySearchConfig(Base):
    """Search-pipeline configuration root."""

    cross_encoder: CrossEncoderConfig = Field(
        default_factory=CrossEncoderConfig,
    )
    sectioning: MemorySearchSectioningConfig = Field(
        default_factory=MemorySearchSectioningConfig,
    )


class MemoryFileWatcherConfig(Base):
    """Background filesystem watcher for ``memory/`` (audit A11 / doc 02 §6.3).

    Default ON. When enabled, the agent loop starts a
    :class:`durin.memory.file_watcher.MemoryFileWatcher` that listens
    for ``.md`` modifications under the workspace's ``memory/``
    directory and triggers ``reindex_one_file`` on each change. Lets
    the user edit memory entries with vim and have the next
    ``memory_search`` see the change without running ``durin memory
    reindex`` manually.

    Disable to skip the watcher (one less background thread + no
    watchdog Observer). Indices stay in sync via the per-tool
    re-index-on-write hooks (memory_store / memory_ingest / Dream).
    """

    enabled: bool = True


class MemoryHealthCheckConfig(Base):
    """Periodic memory subsystem health probe (audit A11 / doc 02 §5.1).

    Default ON. When enabled, the agent loop starts a daemon thread
    that calls :meth:`durin.memory.health_check.HealthChecker.run_tick`
    every ``interval_seconds``. Each tick emits
    ``memory.health_check`` (per A6 — `tick_id`, `duration_ms`,
    `components`, `drift_count`, `errors`) and runs the retention
    pass for telemetry files (P7.2 piggyback).

    Disable to skip the cron (one less background thread). Health
    can still be probed on demand by calling ``run_tick()`` directly
    from a CLI command.
    """

    enabled: bool = True
    interval_seconds: int = Field(default=900, ge=60, le=86_400)


class MemoryConfig(Base):
    """Memory subsystem configuration root.

    ``enabled`` gates vector retrieval. On by default — durin is a
    memory product, so the semantic layer is the default experience.
    `durin onboard` installs the `[memory]` extra and pre-downloads the
    embedding model so it works out of the box; the user can opt out in
    the wizard. When false (or when the `[memory]` extra is missing) the
    memory tools still work over the markdown files (grep-level recall)
    but skip the vector index entirely — no embedding model is loaded,
    and the agent loop warns once at startup so the degradation isn't
    silent (resilient: the embedding model itself auto-downloads on first
    use via fastembed; only the Python extra can't self-install).

    ``dream`` configures auto-trigger of the entity-centric
    :class:`DreamConsolidator` (doc 25 §2.A.1). Independent of
    ``enabled`` — manual ``durin memory dream`` works either way.

    ``search`` configures the search pipeline (cross-encoder etc.).

    ``file_watcher`` and ``health_check`` (audit A11) wire the
    background services the agent loop runs.
    """

    enabled: bool = True
    embedding: MemoryEmbeddingConfig = Field(default_factory=MemoryEmbeddingConfig)
    dream: MemoryDreamConfig = Field(default_factory=MemoryDreamConfig)
    search: MemorySearchConfig = Field(default_factory=MemorySearchConfig)
    file_watcher: MemoryFileWatcherConfig = Field(
        default_factory=MemoryFileWatcherConfig,
    )
    health_check: MemoryHealthCheckConfig = Field(
        default_factory=MemoryHealthCheckConfig,
    )


class AuxModelsConfig(Base):
    """Optional auxiliary models for capability bridges.

    The agent only uses these when the primary model lacks the relevant
    capability AND a config entry is present. Leaving a field unset
    disables the corresponding bridge tool.

    Why no ``pdf`` field: PDF handling does not split cleanly into a
    single auxiliary modality. Some models accept native multimodal
    PDFs, some rasterize pages and consume them through their vision
    path, some prefer pre-extracted text. The capability flag
    (``supports_pdf_input``) is tracked for diagnostics, but text-level
    extraction lives in the dedicated document tool (roadmap #10); when
    the PDF is genuinely visual (scans, diagrams), the vision bridge
    can be used over rasterized pages.
    """

    vision: AuxModelConfig | None = None
    audio: AuxModelConfig | None = None
    # Model used by memory subsystem operations — both dreams
    # (working-memory `dream` and entity-centric `memory_dream`).
    # When unset, dreams fall back to their own `model_override` (if
    # any) and then to the agent's active preset. Lets the user pick
    # a cheaper/faster model for the long offline consolidation runs
    # without changing the chat model.
    memory: AuxModelConfig | None = None


class ModelPresetConfig(Base):
    """A named set of model + generation parameters for quick switching."""

    model: str
    provider: str = "auto"
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    temperature: float = 0.1
    reasoning_effort: str | None = None
    # Pre-emptive compaction trigger (OpenClaw-inspired Tier 2 A1).
    # Fraction of ``context_window_tokens`` above which the consolidator
    # fires BEFORE the next LLM call instead of waiting for a context
    # overflow 400. Per-model because the right value depends on the
    # window: 128K models can sit at 0.5 (compact at 64K); 1M models
    # want ~0.15 (compact at 150K — you pay per token shipped, so
    # waiting until 500K means shipping a huge prompt every turn).
    # ``None`` inherits from ``AgentDefaults.preemptive_compact_ratio``.
    preemptive_compact_ratio: float | None = Field(
        default=None,
        validation_alias=AliasChoices("preemptiveCompactRatio", "preemptive_compact_ratio"),
        serialization_alias="preemptiveCompactRatio",
    )

    def to_generation_settings(self) -> Any:
        from durin.providers.base import GenerationSettings
        return GenerationSettings(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
        )


class ModelCapabilityOverride(Base):
    """User-declared capability override for a specific model name.

    Wins over the vendored snapshot and the heuristic fallback. Use
    when you've added a model the snapshot doesn't know about — for
    example a custom local fine-tune — or when the snapshot is wrong
    for your particular deployment. Any field left as ``None`` falls
    through to the underlying resolver.
    """

    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_vision: bool | None = None
    supports_audio_input: bool | None = None
    supports_pdf_input: bool | None = None
    supports_video_input: bool | None = None
    supports_audio_output: bool | None = None
    supports_image_output: bool | None = None
    supports_function_calling: bool | None = None
    supports_streaming: bool | None = None
    supports_reasoning: bool | None = None
    supports_prompt_caching: bool | None = None
    supports_response_schema: bool | None = None


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "~/.durin/workspace"
    model_preset: str | None = None  # Active preset name — takes precedence over fields below
    model: str = "anthropic/claude-opus-4-5"
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    context_block_limit: int | None = None
    temperature: float = 0.4
    fallback_models: list[FallbackCandidate] = Field(default_factory=list)
    max_tool_iterations: int = 200
    max_concurrent_subagents: int = Field(default=1, ge=1)
    max_tool_result_chars: int = 16_000
    provider_retry_mode: Literal["standard", "persistent"] = "standard"
    tool_hint_max_length: int = Field(
        default=40,
        ge=20,
        le=500,
        validation_alias=AliasChoices("toolHintMaxLength"),
        serialization_alias="toolHintMaxLength",
    )  # Max characters for tool hint display (e.g. "$ cd …/project && npm test")
    reasoning_effort: str | None = None  # low / medium / high / adaptive / none — LLM thinking effort; None preserves the provider default
    timezone: str = "UTC"  # IANA timezone, e.g. "Asia/Shanghai", "America/New_York"
    bot_name: str = "durin"  # Display name shown in CLI prompts (e.g. "{name} is thinking...")
    bot_icon: str = "⚒️"  # Short icon (emoji or text) shown next to the bot name in CLI; "" to omit
    unified_session: bool = False  # Share one session across all channels (single-user multi-device)
    disabled_skills: list[str] = Field(default_factory=list)  # Skill names to exclude from loading (e.g. ["summarize", "skill-creator"])
    max_messages: int = Field(
        default=120,
        ge=0,
    )  # Max messages to replay from session history (0 = use default 120, respects token budget)
    consolidation_ratio: float = Field(
        default=0.5,
        ge=0.1,
        le=0.95,
        validation_alias=AliasChoices("consolidationRatio"),
        serialization_alias="consolidationRatio",
    )  # Consolidation target ratio (0.5 = 50% of budget retained after compression)
    preemptive_compact_ratio: float = Field(
        default=0.5,
        ge=0.05,
        le=0.99,
        validation_alias=AliasChoices("preemptiveCompactRatio", "preemptive_compact_ratio"),
        serialization_alias="preemptiveCompactRatio",
    )  # Tier 2 A1: default trigger ratio when preset doesn't override.
    parallel_tool_calls: dict[str, bool] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("parallelToolCalls", "parallel_tool_calls"),
        serialization_alias="parallelToolCalls",
    )  # Per-model gating for the OpenAI ``parallel_tool_calls`` request flag.
    # Key is a substring of the model name (case-insensitive); value is True/False.
    # First match wins. Empty = preserve the provider default. Use to opt models
    # OUT when they misbehave with parallel tool calls (e.g. {"glm-5.1": false}).
    dream: DreamConfig = Field(default_factory=DreamConfig)


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    # Optional auxiliary models for capability bridges. The agent uses
    # these only when the primary model is known to lack the relevant
    # modality (e.g. GLM has no vision); leaving them unset disables
    # the corresponding bridge tool.
    aux_models: AuxModelsConfig = Field(
        default_factory=AuxModelsConfig,
        validation_alias=AliasChoices("auxModels", "aux_models"),
    )


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str | None = None
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)
    extra_body: dict[str, Any] | None = None  # Extra fields merged into every request body


class BedrockProviderConfig(ProviderConfig):
    """AWS Bedrock Runtime provider configuration."""

    region: str | None = None  # AWS region, falls back to AWS_REGION/AWS_DEFAULT_REGION/profile
    profile: str | None = None  # Optional AWS shared config profile


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    bedrock: BedrockProviderConfig = Field(default_factory=BedrockProviderConfig)  # AWS Bedrock Converse
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    huggingface: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    zai_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # Z.ai Coding Plan (separate quota from zhipu)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    lm_studio: ProviderConfig = Field(default_factory=ProviderConfig)  # LM Studio local models
    atomic_chat: ProviderConfig = Field(default_factory=ProviderConfig)  # Atomic Chat local models
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenVINO Model Server (OVMS)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax_anthropic: ProviderConfig = Field(default_factory=ProviderConfig)  # MiniMax Anthropic endpoint (thinking)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)  # Step Fun (阶跃星辰)
    xiaomi_mimo: ProviderConfig = Field(default_factory=ProviderConfig)  # Xiaomi MIMO (小米)
    longcat: ProviderConfig = Field(default_factory=ProviderConfig)  # LongCat
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True)  # Github Copilot (OAuth)
    qianfan: ProviderConfig = Field(default_factory=ProviderConfig)  # Qianfan (百度千帆)
    nvidia: ProviderConfig = Field(default_factory=ProviderConfig)  # NVIDIA NIM (nvapi- keys)


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    keep_recent_messages: int = 8
    # OpenClaw-inspired: when True, each heartbeat tick runs in a fresh
    # ephemeral session that's deleted after the tick. No state carries
    # between ticks — useful when heartbeat tasks are meant to be
    # stateless one-shots (e.g. "did anything change?") and shouldn't
    # drift due to accumulated context from prior runs. When False
    # (default), the existing behaviour is preserved: one shared session
    # named "heartbeat", trimmed by ``keep_recent_messages`` after each
    # tick.
    isolated_sessions: bool = Field(
        default=False,
        validation_alias=AliasChoices("isolatedSessions", "isolated_sessions"),
        serialization_alias="isolatedSessions",
    )


class ApiConfig(Base):
    """OpenAI-compatible API server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 8900
    timeout: float = 120.0  # Per-request timeout in seconds.


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "127.0.0.1"  # Safer default: local-only bind.
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    # When True, `durin gateway` runs detached (PID file + log file) so
    # the terminal isn't locked. Opt-in because the foreground mode is
    # easier to debug on first install. Toggle via `durin config set
    # gateway.daemon true` or the onboard wizard.
    daemon: bool = False
    # When True, the gateway auto-enables the websocket channel at
    # runtime so the embedded webui is served. Defaults to True because
    # most users running `durin gateway` want the dashboard — toggling
    # this off skips the auto-enable without touching channels.websocket.
    webui_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("webuiEnabled", "webui_enabled"),
        serialization_alias="webuiEnabled",
    )


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all tools; [] = no tools


def _lazy_default(module_path: str, class_name: str) -> Any:
    """Deferred import helper for ToolsConfig default factories."""
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)()


class ToolsConfig(Base):
    """Tools configuration.

    Field types for tool-specific sub-configs are resolved via model_rebuild()
    at the bottom of this file to avoid circular imports (tool modules import
    Base from schema.py).
    """

    web: WebToolsConfig = Field(default_factory=lambda: _lazy_default("durin.agent.tools.web", "WebToolsConfig"))
    exec: ExecToolConfig = Field(default_factory=lambda: _lazy_default("durin.agent.tools.shell", "ExecToolConfig"))
    my: MyToolConfig = Field(default_factory=lambda: _lazy_default("durin.agent.tools.self", "MyToolConfig"))
    restrict_to_workspace: bool = False  # restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    ssrf_whitelist: list[str] = Field(default_factory=list)  # CIDR ranges to exempt from SSRF blocking (e.g. ["100.64.0.0/10"] for Tailscale)


class InstallConfig(Base):
    """Persistent install-level state.

    ``extras`` is the set of optional dependency extras the user has had
    at any point. It's *additive*: durin appends here whenever it detects
    a new importable extra, but never removes entries automatically.
    That way `pipx uninstall` + `pipx install` (which drops extras) gets
    flagged by `durin doctor` instead of silently forgotten.
    """

    extras: list[str] = Field(default_factory=list)


class AppearanceConfig(Base):
    """Visual theme — shared by the TUI and the web dashboard.

    Two axes (see ``design/DESIGN.md``): ``palette`` is the colour
    identity, ``mode`` is light/dark. ``mode = "auto"`` detects the
    terminal (``COLORFGBG``) or the browser's ``prefers-color-scheme``.
    """

    palette: str = "ithildin"  # ithildin | forge | mithril
    mode: str = "auto"  # auto | light | dark


class TelemetryPushConfig(Base):
    """Opt-in HTTPS push of telemetry events (audit A8 / doc 07 §12.2).

    Default OFF. When enabled, every event emitted locally also POSTs
    to ``url`` (buffered, batched per ``batch_size``). The local JSONL
    persistence under ``~/.cache/durin/telemetry/`` runs UNCHANGED —
    push is an ADDITIONAL sink, never a replacement.

    **Privacy**: events carry truncated user content (queries,
    snippets, needles — 200 chars max via ``_truncate_freetext`` in
    ``durin/agent/tools/_telemetry.py``). Enable this only when
    exporting to YOUR OWN infrastructure (Grafana/Loki/Datadog/custom
    endpoint). Read doc 07 §12.2 + §13 before enabling.

    **Auth**: ``token_secret_name`` references a secret stored in
    ``~/.durin/secrets.json`` — NEVER put the bearer token directly
    in ``config.json``. Use ``durin secrets set <name> <token>``.
    """

    enabled: bool = False
    url: str | None = None
    token_secret_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "tokenSecretName", "token_secret_name",
        ),
    )
    batch_size: int = Field(default=10, ge=1, le=1000)


class TelemetryConfig(Base):
    """Telemetry subsystem configuration (audit A8).

    Events emit locally to JSONL by default; optional fan-out to an
    HTTPS endpoint via :class:`TelemetryPushConfig`.
    """

    push: TelemetryPushConfig = Field(default_factory=TelemetryPushConfig)


class Config(BaseSettings):
    """Root configuration for durin."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    appearance: AppearanceConfig = Field(default_factory=AppearanceConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    install: InstallConfig = Field(default_factory=InstallConfig)
    model_presets: dict[str, ModelPresetConfig] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("modelPresets", "model_presets"),
    )
    # Per-model capability overrides — keyed by either the bare model
    # name (``glm-5-turbo``) or the provider-qualified form
    # (``custom/glm-5-turbo``). Provider-qualified keys win over bare
    # names when both match. Values override the vendored snapshot and
    # the heuristic fallback for that model only.
    model_capabilities: dict[str, ModelCapabilityOverride] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("modelCapabilities", "model_capabilities"),
    )

    @model_validator(mode="after")
    def _validate_model_preset(self) -> "Config":
        if "default" in self.model_presets:
            raise ValueError("model_preset name 'default' is reserved for agents.defaults")
        name = self.agents.defaults.model_preset
        if name and name != "default" and name not in self.model_presets:
            raise ValueError(f"model_preset {name!r} not found in model_presets")
        for fallback in self.agents.defaults.fallback_models:
            if isinstance(fallback, str) and fallback not in self.model_presets:
                raise ValueError(f"fallback_models entry {fallback!r} not found in model_presets")
        return self

    def resolve_default_preset(self) -> ModelPresetConfig:
        """Return the implicit `default` preset from agents.defaults fields."""
        d = self.agents.defaults
        return ModelPresetConfig(
            model=d.model, provider=d.provider, max_tokens=d.max_tokens,
            context_window_tokens=d.context_window_tokens,
            temperature=d.temperature, reasoning_effort=d.reasoning_effort,
        )

    def resolve_preset(self, name: str | None = None) -> ModelPresetConfig:
        """Return effective model params from a named preset or the implicit default."""
        name = self.agents.defaults.model_preset if name is None else name
        if not name or name == "default":
            return self.resolve_default_preset()
        if name not in self.model_presets:
            raise KeyError(f"model_preset {name!r} not found in model_presets")
        return self.model_presets[name]

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def _match_provider(
        self, model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from durin.providers.registry import PROVIDERS, find_by_name

        resolved = preset or self.resolve_preset()
        forced = resolved.provider
        if forced != "auto":
            spec = find_by_name(forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                return (p, spec.name) if p else (None, None)
            return None, None

        model_lower = (model or resolved.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or spec.is_direct or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or spec.is_direct or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model, preset=preset)
        return p

    def get_provider_name(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model, preset=preset)
        return name

    def get_api_key(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """Get API key for the given model. Falls back to first available key.

        A ``${secret:NAME}`` reference is resolved through the secret
        store; a literal value passes through unchanged.
        """
        p = self.get_provider(model, preset=preset)
        if not p or not p.api_key:
            return None
        from durin.security.secrets import resolve_secret

        return resolve_secret(p.api_key)

    def get_api_base(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """Get API base URL for the given model, falling back to the provider default when present."""
        from durin.providers.registry import find_by_name

        p, name = self._match_provider(model, preset=preset)
        if p and p.api_base:
            return p.api_base
        if name:
            spec = find_by_name(name)
            if spec and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="DURIN_", env_nested_delimiter="__")


def _resolve_tool_config_refs() -> None:
    """Resolve forward references in ToolsConfig by importing tool config classes.

    Must be called after all modules are loaded (breaks circular imports).
    Re-exports the classes into this module's namespace so existing imports
    like ``from durin.config.schema import ExecToolConfig`` continue to work.
    """
    import sys

    from durin.agent.tools.self import MyToolConfig
    from durin.agent.tools.shell import ExecToolConfig
    from durin.agent.tools.web import WebFetchConfig, WebSearchConfig, WebToolsConfig

    # Re-export into this module's namespace
    mod = sys.modules[__name__]
    mod.ExecToolConfig = ExecToolConfig  # type: ignore[attr-defined]
    mod.WebToolsConfig = WebToolsConfig  # type: ignore[attr-defined]
    mod.WebSearchConfig = WebSearchConfig  # type: ignore[attr-defined]
    mod.WebFetchConfig = WebFetchConfig  # type: ignore[attr-defined]
    mod.MyToolConfig = MyToolConfig  # type: ignore[attr-defined]

    ToolsConfig.model_rebuild()
    Config.model_rebuild()


# Eagerly resolve when the import chain allows it (no circular deps at this
# point).  If it fails (first import triggers a cycle), the rebuild will
# happen lazily when Config/ToolsConfig is first used at runtime.
try:
    _resolve_tool_config_refs()
except ImportError:
    pass
