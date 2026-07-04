"""Configuration schema using Pydantic."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from durin.config.home import durin_home

if TYPE_CHECKING:
    from durin.agent.tools.code_execution import CodeExecutionConfig
    from durin.agent.tools.post_edit_check import PostEditCheckConfig
    from durin.agent.tools.process_registry import ProcessToolConfig
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

    send_progress: bool = Field(default=True, description="Stream the agent's text progress to the channel")
    send_tool_hints: bool = Field(default=False, description='Stream tool-call hints (e.g. read_file("…")) to the channel')
    show_reasoning: bool = Field(default=True, description="Surface model reasoning when the channel implements it")
    send_max_retries: int = Field(default=3, ge=0, le=10, description="Max delivery attempts per message (initial send included)")
    transcription_provider: str = Field(default="groq", description='Voice transcription backend: "groq" or "openai"')
    transcription_language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$", description="Optional ISO-639-1 language hint for audio transcription (e.g. 'en', 'es')")


class InlineFallbackConfig(Base):
    """One inline fallback model configuration."""

    model: str = Field(description="Model identifier to fall back to")
    provider: str = Field(description="Provider name for the fallback model")
    max_tokens: int | None = Field(default=None, description="Max output tokens per turn; None inherits agents.defaults")
    context_window_tokens: int | None = Field(default=None, description="Context window size hint in tokens; None inherits agents.defaults")
    temperature: float | None = Field(default=None, description="Generation temperature; None inherits agents.defaults")
    reasoning_effort: str | None = Field(default=None, description="LLM thinking effort (low/medium/high/adaptive/none); None preserves the provider default")


FallbackCandidate = str | InlineFallbackConfig


class AuxModelConfig(Base):
    """Inline model handle for an auxiliary bridge (vision, audio, pdf, …).

    Either ``preset`` (referencing a named ``model_presets`` entry) or
    the inline ``model`` + ``provider`` pair must be supplied. The
    bridge tools resolve this at call time, so swapping the aux model
    takes effect immediately without a restart.
    """

    preset: str | None = Field(default=None, description="Named model_presets entry to use for this bridge; alternative to inline model + provider")
    model: str | None = Field(default=None, description="Inline model identifier for this bridge; alternative to preset")
    provider: str = Field(default="auto", description='Provider name for the inline model; "auto" auto-detects from the model name')


class TranscriptionLocalConfig(Base):
    """Local on-CPU ASR via sherpa-onnx (spec 2026-06-20)."""

    engine: Literal["parakeet", "sensevoice"] = Field(default="parakeet", description='Local ASR engine: "parakeet" or "sensevoice"')
    model_dir: str | None = Field(default=None, description="Model directory; None auto-downloads to <durin_home>/models/stt/<engine>")
    num_threads: int | None = Field(default=None, description="CPU threads for inference; None uses the provider default (2)")


class TranscriptionHttpConfig(Base):
    """OpenAI-compatible HTTP server endpoint (whisper.cpp, mlx-qwen3-asr, vLLM)."""

    base_url: str | None = Field(default=None, description="Endpoint base URL of the OpenAI-compatible transcription server")
    api_key: str | None = Field(default=None, description="API key for the endpoint; None sends no auth")
    model: str | None = Field(default=None, description="Model name to request from the endpoint")


class TranscriptionProviderKeysConfig(Base):
    """Cloud API credentials for a named provider."""

    api_key: str | None = Field(default=None, description="API key for the provider; prefer ${secret:NAME} references over plaintext")
    api_base: str | None = Field(default=None, description="Optional base URL override for the provider API")


class TranscriptionConfig(Base):
    """Global transcription settings.

    Channel-level ``transcription_provider`` / ``transcription_api_key`` /
    ``transcription_language`` override these per-channel.
    """

    enabled: bool = Field(default=True, description="Master toggle for voice transcription")
    mode: Literal["auto", "preview", "off"] = Field(default="auto", description='"auto" transcribes incoming audio, "preview" shows a transcript without acting on it, "off" disables')
    provider: Literal["local", "openai", "groq", "http"] = Field(default="local", description='Transcription backend: "local" (sherpa-onnx), "openai", "groq", or "http" (OpenAI-compatible endpoint)')
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$", description="Optional ISO-639-1 language hint for transcription")
    local: TranscriptionLocalConfig = Field(default_factory=TranscriptionLocalConfig, description="Local on-CPU ASR engine settings (sherpa-onnx)")
    http: TranscriptionHttpConfig = Field(default_factory=TranscriptionHttpConfig, description="OpenAI-compatible HTTP transcription endpoint settings")
    openai: TranscriptionProviderKeysConfig = Field(default_factory=TranscriptionProviderKeysConfig, description="OpenAI cloud transcription credentials")
    groq: TranscriptionProviderKeysConfig = Field(default_factory=TranscriptionProviderKeysConfig, description="Groq cloud transcription credentials")
    max_duration_s: int = Field(default=600, ge=1, le=86400, description="Maximum audio clip duration in seconds; longer clips are rejected")
    cache_transcripts: bool = Field(default=True, description="Cache transcript results to avoid re-transcribing the same audio")


class TtsLocalConfig(Base):
    """Local on-CPU TTS via Supertonic (ONNX, self-downloading)."""

    engine: Literal["supertonic"] = Field(default="supertonic", description="Local TTS engine identifier (only Supertonic)")
    voice: str = Field(default="F4", description="Preset voice: F1-F5 (female) or M1-M5 (male)")
    model_dir: str | None = Field(default=None, description="Model directory; None lets supertonic auto-download (~260 MB)")
    quality: Literal["normal", "high"] = Field(default="normal", description='Synthesis quality: "normal" = 8 diffusion steps, "high" = 20')


class TtsConfig(Base):
    """Global text-to-speech settings. Sibling of TranscriptionConfig.

    The webui presents this together with `transcription` under one "Voice"
    pane, but the two stay separate flat config blocks (back-compat).
    """

    enabled: bool = Field(default=True, description="Master toggle for text-to-speech")
    provider: Literal["local", "openai"] = Field(default="local", description='TTS backend: "local" (Supertonic) or "openai" (cloud)')
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$", description="Optional ISO-639-1 language hint; None = auto")
    fallback: Literal["none", "openai"] = Field(default="none", description='"openai" falls through to cloud TTS when local synthesis fails; "none" disables the fallback')
    local: TtsLocalConfig = Field(default_factory=TtsLocalConfig, description="Local on-CPU TTS engine settings (Supertonic)")
    openai: TranscriptionProviderKeysConfig = Field(
        default_factory=TranscriptionProviderKeysConfig,
        description="OpenAI cloud TTS credentials",
    )


class SpokenRenderConfig(Base):
    """How long replies are rendered for speech (spoken != displayed)."""

    mode: Literal["model_led", "verbatim"] = Field(default="model_led", description='"model_led" speaks the opening and leaves the rest on screen; "verbatim" reads everything')
    long_threshold_words: int = Field(default=60, ge=1, description="Replies at or under this many words are always read in full")
    pointer: str = Field(default="The full answer is on screen.", description="Sentence appended after the opening in model_led mode")

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_mode(cls, data: Any) -> Any:
        # An earlier build offered an "aux_summary" mode that was never wired
        # and always degraded to "model_led". Coerce any value persisted by that
        # build so existing configs keep loading with identical behavior.
        if isinstance(data, dict) and data.get("mode") == "aux_summary":
            data = {**data, "mode": "model_led"}
        return data


class VoiceConfig(Base):
    """Hands-free conversational voice mode (the gateway loop)."""

    enabled: bool = Field(default=True, description="Master toggle for conversational voice mode")
    barge_in: bool = Field(default=True, description="Allow interrupting playback by speaking over it")
    vad_threshold: float = Field(default=0.5, ge=0.0, le=1.0, description="Browser voice-activity-detection sensitivity, 0-1 (relayed to the browser VAD)")
    end_of_turn_silence_ms: int = Field(default=700, ge=100, description="Silence in milliseconds that ends an utterance (relayed to the browser VAD)")
    idle_timeout_s: int = Field(default=300, ge=0, description="Auto-exit voice mode after this many seconds of silence; 0 = never")
    spoken_render: SpokenRenderConfig = Field(default_factory=SpokenRenderConfig, description="How long replies are rendered for speech (spoken text differs from displayed text)")


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

    provider: str = Field(default="fastembed", description='Embedding adapter; currently only "fastembed"')
    # Default model: multilingual-e5-small (registered as custom model
    # in durin/memory/embedding.py::_CUSTOM_MODELS). 117M params, 384-
    # dim, 100+ languages, MIT, retrieval-tuned. Replaced
    # paraphrase-multilingual-MiniLM-L12-v2; see `_EMBEDDING_CHOICES` in
    # the onboarding wizard for the rationale.
    model: str = Field(default="intfloat/multilingual-e5-small", description="Embedding model name; must exist in fastembed's catalog for the installed version")
    base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("baseUrl", "base_url"),
        description="HTTP embedding provider base URL (reserved for future adapters; fastembed ignores it)",
    )
    api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("apiKey", "api_key"),
        description="HTTP embedding provider API key (reserved for future adapters; fastembed ignores it)",
    )
    lazy_eviction: bool = Field(
        default=False,
        validation_alias=AliasChoices("lazyEviction", "lazy_eviction"),
        description="Reserved: evict the embedding model when idle; V1 keeps it resident for the life of the process",
    )


class MemoryDreamConfig(Base):
    """Memory dream config: when the extract / refine / skill passes run.

    Governs the daily ``memory_dream`` cron plus two reactive triggers. The
    manual ``durin memory dream`` always works regardless of ``enabled``.

    Three triggers (any combination):

    - **cron**: daily schedule (predictable).
    - **post_compaction**: dream after a session is compacted — the context
      is already in memory so the cost is amortised.
    - **on_session_close**: dream when a session ends (``/quit`` or idle
      timeout).
    """

    enabled: bool = Field(default=True, description="Master switch; false disables the cron + reactive triggers (manual `durin memory dream` still works)")

    cron: str = Field(default="0 3 * * *", description="Cron expression for the daily extract / refine / skill pass")

    post_compaction: bool = Field(default=True, description="Run a dream pass after a session is compacted (the context is already in memory, so the cost is amortised)")

    on_session_close: bool = Field(default=True, description="Run a dream pass when a session ends (/quit or idle timeout)")

    # ON by default: the failure mode is additive (a low-signal page,
    # overridable + git-revertable), far milder than a destructive merge.
    # Precision lives in the discovery prompt; `memory.dream.discover`
    # telemetry measures it.
    discover_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("discoverEnabled", "discover_enabled"),
        description="Mention-based entity discovery (extract stage 2): also create/update entities from durable facts the agent mentioned but did not explicitly upsert",
    )

    # ON by default: detection only (curation decides); precision lives in the
    # prompt and `memory.dream.skill_signals` telemetry measures it.
    skill_signals_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("skillSignalsEnabled", "skill_signals_enabled"),
        description="Hindsight skill-signal extraction (extract stage 3): detect skill corrections/gaps from session turns and log them as observations for the daily curation pass",
    )

    # ON by default: additive only (writes feedback entities);
    # `memory.dream.learnings` telemetry measures precision.
    learnings_sweep_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("learningsSweepEnabled", "learnings_sweep_enabled"),
        description="Durable-learnings sweep (extract stage 4): mine each session's new turns for corrections, preferences, and project facts and write them as feedback entities",
    )

    # ON by default: nothing is applied without explicit user acceptance.
    # Auto skills are unaffected by this toggle.
    skill_suggestions_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "skillSuggestionsEnabled", "skill_suggestions_enabled"),
        description="Also evaluate MANUAL workspace skills in the daily curation and enqueue proposed edits as suggestions for user review (never auto-applied)",
    )

    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model_override"),
        description="DEPRECATED — prefer agents.aux_models.memory, which pairs the model with its provider. A bare name set here is placed by provider auto-detection from the name; None falls through to agents.aux_models.memory and then the default model",
    )

    # The reactive triggers fire on a daemon thread per event; a burst of
    # session closes would otherwise spawn overlapping extract passes. The
    # per-session cursor means skipped turns are picked up by the next run.
    min_seconds_between_runs: int = Field(
        default=300,
        ge=0,
        validation_alias=AliasChoices("minSecondsBetweenRuns", "min_seconds_between_runs"),
        description="Throttle window in seconds for the reactive triggers (post_compaction + on_session_close); 0 disables the throttle; the daily cron is never throttled",
    )

    # The per-session cursor makes the remainder resume on the next trigger.
    max_seconds_per_run: int = Field(
        default=600,
        ge=0,
        validation_alias=AliasChoices("maxSecondsPerRun", "max_seconds_per_run"),
        description="Wall-clock cap in seconds per extract pass; the pass yields after the current session when crossed; 0 = run to completion",
    )

    # Injected into EVERY prompt, so this is a per-turn cost. Default 1500 ≈
    # 15-25 concise standing instructions.
    always_on_token_budget: int = Field(
        default=1500,
        ge=0,
        validation_alias=AliasChoices("alwaysOnTokenBudget", "always_on_token_budget"),
        description="Token budget for the always-on guidance pin distilled from feedback and injected into every prompt; 0 disables the pin",
    )

    auto_absorb: "AutoAbsorbConfig" = Field(
        default_factory=lambda: AutoAbsorbConfig(),
        validation_alias=AliasChoices("autoAbsorb", "auto_absorb"),
        description="Post-dream automatic entity deduplication (the refine pass's judged merge)",
    )


class AutoAbsorbConfig(Base):
    """Auto-absorb post-dream config.

    After a successful dream pass, optionally run an LLM-judge over
    alias-overlap candidates and auto-merge those above the confidence
    threshold. Designed to close the loop between dream consolidation
    and manual ``durin memory absorb`` without destructive false-merges.

    Enabled by default: the high-precision judge (≥95), prevent-at-source
    dedup, and the run-scoped quarantine together contain the blast radius
    that originally justified opt-in. (Refine additionally never merges
    entities created during the current run, so a pass can't merge its own
    fresh output.) Disable to require manual ``durin memory absorb``.

    The merge itself reuses :meth:`EntityAbsorption.absorb` (which
    preserves content from both pages via ``_merge_pages``, archives
    the absorbed page under ``entities/<type>/<canonical>/archive/``,
    and records the action in a git commit with full reasoning in the
    trailers). Recovery: ``cd memory && git revert <sha>``.
    """

    # ON by default: prevent-at-source (lexical + semantic dedup) + the
    # run-scoped quarantine + the judge (≥95) + the reversible archive contain
    # the blast radius that justified opt-in. A bad merge is recoverable: git
    # revert the absorb commit in memory/ and add a tombstone.
    enabled: bool = Field(default=True, description="Auto-merge judged entity duplicates after a dream pass; false requires manual `durin memory absorb`")

    # Default 95 favours precision over recall: most pairs that warrant a
    # merge will also warrant manual review at this threshold. Tune down with
    # data from ``memory.absorb.judged`` telemetry.
    confidence_threshold: int = Field(
        default=95,
        ge=0,
        le=100,
        validation_alias=AliasChoices("confidenceThreshold", "confidence_threshold"),
        description="LLM-judge confidence floor (0-100) required for an auto-merge",
    )

    # 0.30 ≈ cosine 0.85; the judge (confidence_threshold) decides the actual
    # merge/reuse, so recall favours catching near-duplicate variant names
    # over minimising judge calls.
    semantic_distance_threshold: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,  # L2² of 1.0 ≈ cosine 0.5 — already far looser than useful;
        # nothing above it is a meaningful dedup candidate. Matches the webui input.
        validation_alias=AliasChoices(
            "semanticDistanceThreshold", "semantic_distance_threshold"),
        description="LanceDB L2² distance below which an embedding-near same-type entity becomes a dedup candidate handed to the judge (refine + discovery)",
    )

    escalate_floor: int = Field(
        default=0,
        ge=0,
        le=100,
        validation_alias=AliasChoices("escalateFloor", "escalate_floor"),
        description="Confidence floor above which pairs the cheap judge can't settle (verdict 'unclear', or 'same' below confidence_threshold) escalate to a bounded investigating sub-agent; 0 disables escalation",
    )


class CrossEncoderConfig(Base):
    """Cross-encoder reranker config.

    OFF by default — but this is a *library/CI-safe* default, NOT a
    recommendation against it. Enabling it triggers a one-time
    multi-hundred-MB model download on first search, which must not
    happen implicitly in CI / test / headless environments that build a
    default config. The onboarding wizard recommends turning it ON (it
    asks the operator explicitly, with the download + latency cost
    stated) because in the real personal-agent loop the rerank's
    300-800ms is dwarfed by the multi-second LLM call that follows every
    search, while the ranking gain is direct. Edge / RAM-constrained
    operators decline at the prompt. The pipeline degrades gracefully to
    RRF + entity-aware rerank when disabled or when the model fails to
    load.
    """

    enabled: bool = Field(default=False, description="Enable the cross-encoder reranker; triggers a one-time multi-hundred-MB model download on first search")
    # Default model: BAAI/bge-reranker-base (MIT, ~100M params,
    # multilingual). Switched from jinaai/jina-reranker-v2-base-
    # multilingual on 2026-05-30 — see H30 note in
    # `durin/memory/cross_encoder.py`. Override to one of the curated
    # alternatives (bge-v2-m3 for heavy multilingual, others) if you
    # want a different trade-off.
    model: str = Field(default="BAAI/bge-reranker-base", description="Reranker model name (MIT, multilingual by default)")
    batch_size: int = Field(default=32, description="Query-document pairs scored per inference batch")
    top_n: int = Field(default=10, description="Top-N hits kept after the rerank step")


class MemorySearchSectioningConfig(Base):
    """Sectioning step configuration.

    ``max_per_source`` caps how many `corpus` hits sharing the same
    `ingest_id` can survive the sectioning step. Default 3 — set
    when an ingested document is chunked into many corpus entries
    and a single semantic query would otherwise monopolise the
    top-K with consecutive chunks of the same source.

    The value was previously hard-coded in
    `durin.memory.sectioned_output.DEFAULT_MAX_PER_SOURCE`; it is now
    configurable. Default unchanged so existing workspaces see zero
    behaviour change.
    """

    max_per_source: int = Field(default=3, description="Max corpus hits sharing the same ingest_id that survive the sectioning step, so one chunked document can't monopolise the top-K")


class MemorySearchConfig(Base):
    """Search-pipeline configuration root."""

    cross_encoder: CrossEncoderConfig = Field(
        default_factory=CrossEncoderConfig,
        description="Cross-encoder reranker step settings",
    )
    sectioning: MemorySearchSectioningConfig = Field(
        default_factory=MemorySearchSectioningConfig,
        description="Sectioning step settings (per-source result caps)",
    )


class MemoryFileWatcherConfig(Base):
    """Background filesystem watcher for ``memory/``.

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

    enabled: bool = Field(default=True, description="Watch memory/*.md for manual edits and re-index changed files automatically; disable for one less background thread")


class MemoryHealthCheckConfig(Base):
    """Periodic memory subsystem health probe.

    Default ON. When enabled, the agent loop starts a daemon thread
    that calls :meth:`durin.memory.health_check.HealthChecker.run_tick`
    every ``interval_seconds``. Each tick emits
    ``memory.health_check`` (``tick_id``, ``duration_ms``,
    ``components``, ``drift_count``, ``errors``) and runs the retention
    pass for telemetry files.

    Disable to skip the cron (one less background thread). Health
    can still be probed on demand by calling ``run_tick()`` directly
    from a CLI command.
    """

    enabled: bool = Field(default=True, description="Run the periodic memory health probe; disable for one less background thread")
    interval_seconds: int = Field(default=900, ge=60, le=86_400, description="Probe interval in seconds")


class SkillsHotTierConfig(Base):
    """Hot working-set tier for skills.

    The cache-stable prefix injects only the usage-ranked working set
    instead of the whole catalog; the long tail is reachable via
    ``memory_search`` (kind="skill"). ``enabled=False`` restores the
    full-catalog injection (A/B / calibration fallback). Sizes favor
    frequent-over-the-window (the durable working set); ``recent`` is a
    smaller recency bonus. Calibrate with the shipped ``memory.skill_miss``
    telemetry.
    """

    enabled: bool = Field(default=True, description="Inject only the usage-ranked working set of skills instead of the full catalog; false restores full-catalog injection")
    recent: int = Field(default=15, description="Number of recently-used skills included in the working set")
    frequent: int = Field(default=30, description="Number of frequently-used skills included in the working set")
    frequent_window_hours: float = Field(default=168.0, description="Window in hours for the frequency ranking (default 7 days)")
    recent_window_hours: float = Field(default=24.0, description="Window in hours for the recency ranking")


class SkillJudgeConfig(Base):
    """LLM semantic-audit pass over an imported skill, after the deterministic
    AST scan. ``trigger`` decides WHEN it auto-runs:
    ``off`` (default) — never auto; invoke on-demand per skill ("Audit with LLM").
    ``uncertain`` — only when the gate is already unsure (carries code / caution /
    out-of-allowlist), to break the tie; clean allowlisted skills skip it (zero
    tax). ``always`` — every import. An LLM call per import is overkill for the
    common case (the human already reviews + approves), hence ``off`` default.
    Degrades gracefully (skips, never errors/blocks) when no aux model resolves.
    ``max_severity`` caps how high the judge may raise the verdict: ``caution``
    (default) lets it force a confirm but never block on its own — only the
    deterministic rules block. ``model`` names an aux model; empty → default."""

    trigger: Literal["off", "uncertain", "always"] = Field(default="off", description='When the LLM audit auto-runs on import: "off" = only on demand, "uncertain" = only when the deterministic gate is unsure, "always" = every import')
    max_severity: Literal["caution", "dangerous"] = Field(default="caution", description='Cap on how high the judge may raise the verdict: "caution" can force a confirm but never block; only deterministic rules block')
    model: str = Field(default="", description="Aux model name for the judge; empty = default aux model")
    provider: str = Field(default="auto", description='Provider for the judge model; "auto" detects it from the model name among the configured providers (a bare model name is meaningless without its provider)')


# Skill-source prefixes trusted by default — verified first-party vendor orgs +
# de-facto-standard frameworks on skills.sh (2026-06). A match only skips the
# *source* confirmation; the verdict/code gates still apply (so this never
# auto-installs code-carrying or non-safe skills). Editable in config + the webui
# (Skills security → Trust patterns). `github`/`microsoft` are scoped to their
# skills repo (both are giant general orgs); the rest are org-level.
DEFAULT_SKILL_ALLOWLIST: list[str] = [
    "github:anthropics/",                # Anthropic — official Agent Skills
    "github:google-gemini/",             # Google — official Gemini skills
    "github:openai/",                    # OpenAI — official skills catalog
    "github:vercel-labs/",               # Vercel — official agent-skills
    "github:nousresearch/",              # Nous Research — hermes-agent skills
    "github:obra/",                      # superpowers — de-facto-standard framework
    "github:flutter/",                   # Google — Flutter skills
    "github:firebase/",                  # Google — Firebase agent-skills
    "github:googleworkspace/",           # Google — Workspace CLI skills
    "github:google-labs-code/",          # Google Labs — stitch-skills
    "github:microsoft/azure-skills/",    # Microsoft — Azure skills (repo-scoped)
    "github:github/awesome-copilot/",    # GitHub/Microsoft — Copilot hub (repo-scoped)
]


class SkillSecurityConfig(Base):
    """Security floor + policy for skill import. ``allowlist`` =
    trusted source-ref prefixes (e.g. ``github:anthropics/``). A match skips only
    the *source* confirmation; the verdict/code gates have no opt-out. Ships with a
    vetted default of first-party vendor + de-facto orgs (``DEFAULT_SKILL_ALLOWLIST``),
    editable in config + the webui (Skills security → Trust patterns). Caps bound a
    fetched skill's size/file count. ``github_token_secret`` names a durin secret
    holding a GitHub API token (raises rate limits + private repos); empty →
    anonymous. (Running a skill's declared dependency installs is governed by
    ``skills.install_policy`` — see P6 #1.)"""

    allowlist: list[str] = Field(default_factory=lambda: list(DEFAULT_SKILL_ALLOWLIST), description="Trusted source-ref prefixes (e.g. 'github:anthropics/'); a match skips only the source confirmation, never the verdict/code gates")
    github_token_secret: str = Field(default="", description="Durin secret name holding a GitHub API token (raises rate limits + enables private repos); empty = anonymous")
    max_files: int = Field(default=100, description="Maximum file count in a fetched skill")
    max_total_bytes: int = Field(default=3 * 1024 * 1024, description="Maximum total size in bytes of a fetched skill (3 MB)")
    max_file_bytes: int = Field(default=1024 * 1024, description="Maximum size in bytes of a single skill file (1 MB)")
    llm_judge: SkillJudgeConfig = Field(default_factory=SkillJudgeConfig, description="Optional LLM semantic-audit pass over imported skills")


class SkillRegistryConfig(Base):
    """One search registry. ``kind`` selects the adapter; ``api_key_secret`` names
    a durin secret (empty → anonymous). ``taps`` is github-only (repos to search)."""

    name: str = Field(description="Registry display name")
    kind: Literal["skills.sh", "clawhub", "github", "well-known"] = Field(description='Adapter for this registry: "skills.sh", "clawhub", "github", or "well-known"')
    enabled: bool = Field(default=True, description="Include this registry in skill searches")
    api_key_secret: str = Field(default="", description="Durin secret name holding the registry API key; empty = anonymous")
    taps: list[str] = Field(default_factory=list, description="GitHub-only: repos to search for skills")


class SkillsDiscoveryConfig(Base):
    """Skill discovery: which registries to search + how many results."""

    registries: list[SkillRegistryConfig] = Field(
        default_factory=lambda: [
            SkillRegistryConfig(name="skills.sh", kind="skills.sh"),
            SkillRegistryConfig(name="clawhub", kind="clawhub"),
        ],
        description="Skill registries to search, in order",
    )
    search_limit: int = Field(default=10, description="Max results returned per skill search")


class McpRegistryConfig(Base):
    """One MCP registry. ``kind`` selects the adapter; ``api_key_secret`` names a
    durin secret (unused for the no-auth official registry; reserved for future)."""

    name: str = Field(description="Registry display name")
    kind: Literal["official", "mpak"] = Field(description='Adapter for this MCP registry: "official" or "mpak"')
    enabled: bool = Field(default=True, description="Include this registry in MCP server searches")
    api_key_secret: str = Field(default="", description="Durin secret name holding the registry API key; unused for the no-auth official registry (reserved)")


class McpDiscoveryConfig(Base):
    """MCP server discovery: which registries to search, result cap, install gate.

    ``install_policy`` is MCP-owned (separate from ``SkillsConfig.install_policy``)
    because adding/running an MCP server is a distinct trust surface; same literal,
    independent value. mpak ships disabled by default — fast-follow (its trust score
    lives in a native endpoint, not the spec-compatible ``/v0.1/servers``)."""

    registries: list[McpRegistryConfig] = Field(
        default_factory=lambda: [McpRegistryConfig(name="official", kind="official")],
        description="MCP registries to search, in order",
    )
    search_limit: int = Field(default=10, description="Max results returned per MCP server search")
    install_policy: Literal["never", "approve", "auto"] = Field(default="approve", description='Gate on installing a discovered MCP server: "never", "approve" (per-install confirm), or "auto"')
    quality: Literal["official", "all"] = Field(default="official", description="Default discovery view. 'official' applies the star/first-party gate; 'all' returns the full registry")
    min_stars: int = Field(default=100, description="Star floor for the 'official' gate")


class SkillsConfig(Base):
    """Global skill-subsystem governance. Per-agent skill-context tuning
    (``skills_hot_tier``, ``disabled_skills``) lives on ``agents.defaults``;
    the memory-index toggle stays at ``memory.index_skills``.
    ``discovery`` (registries + search) is configured here."""

    security: SkillSecurityConfig = Field(default_factory=SkillSecurityConfig, description="Skill-import security floor: allowlist, size caps, LLM judge")
    discovery: SkillsDiscoveryConfig = Field(default_factory=SkillsDiscoveryConfig, description="Skill discovery registries and search limits")
    install_policy: Literal["never", "approve", "auto"] = Field(
        default="approve",
        description="How `skill_install_deps` runs a skill's declared install specs: 'never' = report only, 'approve' = dry-run then run on confirm, 'auto' = run without a per-call confirm; all policies still execute through ExecTool's gate",
    )


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

    ``dream`` configures the entity-centric dream passes (extract / refine /
    skill / always_on) and their cron + reactive triggers.
    Manual ``durin memory dream`` works regardless of the triggers.

    ``search`` configures the search pipeline (cross-encoder etc.).

    ``file_watcher`` and ``health_check`` wire the background services
    the agent loop runs.
    """

    enabled: bool = Field(default=True, description="Enable vector retrieval; when false the memory tools still work over the markdown files (grep-level recall) but skip the vector index")
    index_skills: bool = Field(default=True, description="Make skills searchable as a `skill` memory class (skills are authored + injected regardless)")
    owner: str | None = Field(default=None, description='Workspace owner entity ref (e.g. "person:marcelo") used to resolve the principal for the pinned context; None defaults to anonymous')
    embedding: MemoryEmbeddingConfig = Field(default_factory=MemoryEmbeddingConfig, description="Embedding model for the vector index")
    dream: MemoryDreamConfig = Field(default_factory=MemoryDreamConfig, description="Dream passes (extract / refine / skill / always_on) and their cron + reactive triggers")
    search: MemorySearchConfig = Field(default_factory=MemorySearchConfig, description="Search pipeline settings (cross-encoder reranker, sectioning)")
    file_watcher: MemoryFileWatcherConfig = Field(
        default_factory=MemoryFileWatcherConfig,
        description="Background filesystem watcher that re-indexes manually edited memory/*.md files",
    )
    health_check: MemoryHealthCheckConfig = Field(
        default_factory=MemoryHealthCheckConfig,
        description="Periodic memory subsystem health probe",
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

    vision: AuxModelConfig | None = Field(default=None, description="Aux model for vision inputs when the primary model lacks vision; unset disables the bridge")
    audio: AuxModelConfig | None = Field(default=None, description="Aux model for audio inputs when the primary model lacks audio; unset disables the bridge")
    memory: AuxModelConfig | None = Field(default=None, description="Model for the memory dream passes; overrides memory.dream.model_override; unset falls through to the dream's own resolution")
    # Resolved fresh at each spawn, so a hot-reloaded change takes effect
    # immediately.
    subagents: AuxModelConfig | None = Field(default=None, description="Model for spawned subagents (background spawn/tasks runs); unset = the subagent inherits the parent session's model")


class ModelPresetConfig(Base):
    """A named set of model + generation parameters for quick switching."""

    model: str = Field(description="Model identifier")
    provider: str = Field(default="auto", description='Provider name or "auto" for auto-detection')
    max_tokens: int = Field(default=8192, description="Max output tokens per turn")
    context_window_tokens: int = Field(default=65_536, description="Context window size hint in tokens")
    temperature: float = Field(default=0.1, description="Generation temperature")
    reasoning_effort: str | None = Field(default=None, description="LLM thinking effort (low/medium/high/adaptive/none); None preserves the provider default")
    request_timeout_s: float | None = Field(default=None, description="Per-model HTTP timeout in seconds; overrides DURIN_OPENAI_COMPAT_TIMEOUT_S")
    top_p: float | None = Field(default=None, description="Nucleus sampling (standard OpenAI param); None = don't send")
    top_k: int | None = Field(default=None, description="Top-k sampling; non-standard, sent via extra_body (ollama / LM Studio)")
    repeat_penalty: float | None = Field(default=None, description="Repetition penalty; non-standard, sent via extra_body")
    # Per-model because the right value depends on the window: 128K models
    # can sit at 0.5 (compact at 64K); 1M models want ~0.15 (compact at
    # 150K — you pay per token shipped, so waiting until 500K means
    # shipping a huge prompt every turn).
    preemptive_compact_ratio: float | None = Field(
        default=None,
        validation_alias=AliasChoices("preemptiveCompactRatio", "preemptive_compact_ratio"),
        serialization_alias="preemptiveCompactRatio",
        description="Fraction of context_window_tokens above which compaction fires before the next LLM call instead of waiting for a context-overflow 400; None inherits agents.defaults.preemptive_compact_ratio",
    )

    def to_generation_settings(self) -> Any:
        from durin.providers.base import GenerationSettings
        return GenerationSettings(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
            top_p=self.top_p,
            top_k=self.top_k,
            repeat_penalty=self.repeat_penalty,
        )


class PersonaConfig(Base):
    """A named persona: a SOUL plus an optional model, selectable for a chat or
    a cron job. ``soul`` is a SoulStore slug (``default`` = the workspace
    SOUL.md). ``model`` is a model picker ref (a preset name or a
    ``"provider model"`` pair); ``None`` means use the global default model."""

    soul: str = Field(default="default", description='SoulStore slug for the persona\'s SOUL; "default" = the workspace SOUL.md')
    model: str | None = Field(default=None, description='Model picker ref (a preset name or a "provider model" pair); None uses the global default model')
    description: str | None = Field(default=None, description="Human-readable description shown in the persona picker")


class ModeConfig(Base):
    """A user-defined agent mode, persisted in config and registered into the
    agent-mode registry at startup (the dict key is the mode name).

    Mirrors ``AgentMode``'s data knobs: ``allowed`` of ``None`` means full
    access (every tool) unless ``denied``; a list means only those tools are
    allowed. ``denied`` always wins. ``prompt_suffix`` is appended to the system
    prompt while the mode is active. ``icon`` is an optional glyph name for the
    UI; the picker falls back to a generic glyph when it is unset.
    """

    description: str = Field(default="", description="Human-readable description shown in the mode picker")
    allowed: list[str] | None = Field(default=None, description="Tool names allowed while the mode is active; None = full access (every tool) unless denied")
    denied: list[str] = Field(default_factory=list, description="Tool names denied while the mode is active; denied always wins over allowed")
    prompt_suffix: str = Field(default="", description="Text appended to the system prompt while the mode is active")
    icon: str | None = Field(default=None, description="Optional glyph name for the UI; the picker falls back to a generic glyph when unset")


class ModelCapabilityOverride(Base):
    """User-declared capability override for a specific model name.

    Wins over the vendored snapshot and the heuristic fallback. Use
    when you've added a model the snapshot doesn't know about — for
    example a custom local fine-tune — or when the snapshot is wrong
    for your particular deployment. Any field left as ``None`` falls
    through to the underlying resolver.
    """

    max_input_tokens: int | None = Field(default=None, description="Override for the model's max input tokens; None falls through to the resolver")
    max_output_tokens: int | None = Field(default=None, description="Override for the model's max output tokens; None falls through to the resolver")
    supports_vision: bool | None = Field(default=None, description="Override: model accepts image inputs; None falls through to the resolver")
    supports_audio_input: bool | None = Field(default=None, description="Override: model accepts audio inputs; None falls through to the resolver")
    supports_pdf_input: bool | None = Field(default=None, description="Override: model accepts native PDF inputs; None falls through to the resolver")
    supports_video_input: bool | None = Field(default=None, description="Override: model accepts video inputs; None falls through to the resolver")
    supports_audio_output: bool | None = Field(default=None, description="Override: model can produce audio output; None falls through to the resolver")
    supports_image_output: bool | None = Field(default=None, description="Override: model can produce image output; None falls through to the resolver")
    supports_function_calling: bool | None = Field(default=None, description="Override: model supports tool/function calling; None falls through to the resolver")
    supports_streaming: bool | None = Field(default=None, description="Override: model supports streamed responses; None falls through to the resolver")
    supports_reasoning: bool | None = Field(default=None, description="Override: model supports reasoning/thinking; None falls through to the resolver")
    supports_prompt_caching: bool | None = Field(default=None, description="Override: model supports prompt caching; None falls through to the resolver")
    supports_response_schema: bool | None = Field(default=None, description="Override: model supports structured response schemas; None falls through to the resolver")


class AgentDefaults(Base):
    """Default agent configuration."""

    # Resolved at instance time so a fresh config under DURIN_HOME points at its
    # own workspace; an existing config keeps the value persisted on disk.
    workspace: str = Field(default_factory=lambda: str(durin_home() / "workspace"), description="Working directory for file tools; defaults to <durin_home>/workspace")
    model_preset: str | None = Field(default=None, description="Active model_presets entry name; takes precedence over the model/provider fields below")
    persona: str | None = Field(default=None, description="Default persona name for interactive chats; None = the workspace SOUL + default model")
    personas_seeded: bool = Field(default=False, description="Set once the example personas have been seeded into `personas` (managed by durin)")
    model: str = Field(default="anthropic/claude-opus-4-5", description="Active model identifier (provider/name form)")
    provider: str = Field(default="auto", description='Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection from the model name')
    max_tokens: int = Field(default=8192, description="Max output tokens per turn")
    context_window_tokens: int = Field(default=65_536, description="Context window size hint in tokens")
    context_block_limit: int | None = Field(default=None, description="Hard limit on context blocks; overrides the token budget when set")
    temperature: float = Field(default=0.4, description="Generation temperature")
    fallback_models: list[FallbackCandidate] = Field(default_factory=list, description="Ordered list of preset names or inline model specs to try on provider failure")
    max_tool_iterations: int = Field(default=200, description="Cap on tool-call iterations per turn")
    # Default 3 (not 1): the main agent can run a few independent subagents at
    # once — the common "fan out 2-3 research/explore tasks" pattern — while
    # still bounding cost, since each subagent is a full LLM loop.
    max_concurrent_subagents: int = Field(default=3, ge=1, description="Parallel subagent concurrency cap; 1 forces strictly serial subagents")
    max_concurrent_interactive: int = Field(default=4, ge=1, description="Interactive-lane cap: human-facing turns running at once across all sessions; env DURIN_MAX_CONCURRENT_REQUESTS overrides at runtime")
    concurrency_ceiling: int = Field(default=12, ge=1, description="Global ceiling on total in-flight turns + subagents across all lanes; keep it >= the interactive cap")
    max_tool_result_chars: int = Field(default=16_000, description="Truncation limit in characters on individual tool results")
    provider_retry_mode: Literal["standard", "persistent"] = Field(default="standard", description='Retry strategy on provider errors: "standard" or "persistent"')
    tool_hint_max_length: int = Field(
        default=40,
        ge=20,
        le=500,
        validation_alias=AliasChoices("toolHintMaxLength"),
        serialization_alias="toolHintMaxLength",
        description='Max characters for tool-call hint display (e.g. "$ cd …/project && npm test")',
    )
    reasoning_effort: str | None = Field(default=None, description="LLM thinking effort: low / medium / high / adaptive / none; None preserves the provider default")
    timezone: str = Field(default="UTC", description='IANA timezone for date-aware behavior, e.g. "Asia/Shanghai", "America/New_York"')
    bot_name: str = Field(default="durin", description='Display name shown in CLI prompts (e.g. "{name} is thinking...")')
    bot_icon: str = Field(default="⚒️", description='Short icon (emoji or text) shown next to the bot name in CLI; "" to omit')
    unified_session: bool = Field(default=False, description="Share one session across all channels (single-user multi-device)")
    ask_user_blocking: bool = Field(default=True, description="ask_user awaits the user's next message inside the same turn instead of yielding; on timeout it degrades to yield semantics")
    ask_user_answer_timeout_s: int = Field(default=300, ge=10, le=3600, description="Seconds a blocking ask_user waits for an answer before degrading to yield")
    plan_stall_turns: int = Field(
        default=8, ge=0,
        description='Turns without todo progress while executing an approved plan before a "reassess" reminder is injected into Runtime Context; 0 disables',
    )
    disabled_skills: list[str] = Field(default_factory=list, description='Skill names to exclude from loading (e.g. ["summarize", "skill-creator"])')
    skills_hot_tier: SkillsHotTierConfig = Field(default_factory=SkillsHotTierConfig, description="Hot working-set tier for skill injection (usage-ranked subset instead of the full catalog)")
    max_messages: int = Field(
        default=120,
        ge=0,
        description="Max messages replayed from session history (0 = use default 120); the token budget still applies",
    )
    consolidation_ratio: float = Field(
        default=0.5,
        ge=0.1,
        le=0.95,
        validation_alias=AliasChoices("consolidationRatio"),
        serialization_alias="consolidationRatio",
        description="Consolidation target ratio: fraction of the context budget retained after compression (0.5 = 50%)",
    )
    preemptive_compact_ratio: float = Field(
        default=0.5,
        ge=0.05,
        le=0.99,
        validation_alias=AliasChoices("preemptiveCompactRatio", "preemptive_compact_ratio"),
        serialization_alias="preemptiveCompactRatio",
        description="Default fraction of the context window that triggers pre-emptive compaction when the active preset doesn't override it",
    )
    decision_log_enabled: bool = Field(default=True, description="Record key decisions/findings in a task-state anchor that survives compaction")
    compaction_learnings_enabled: bool = Field(default=True, description="Distil durable user learnings (preferences, corrections) at compaction time")
    decision_log_max_entries: int = Field(default=10, ge=1, le=100, description="Cap on decision-log entries (the log is re-injected every turn)")
    decision_log_max_chars: int = Field(default=1500, ge=100, le=20_000, description="Total character cap on the decision log")
    parallel_tool_calls: dict[str, bool] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("parallelToolCalls", "parallel_tool_calls"),
        serialization_alias="parallelToolCalls",
        description='Per-model gating for the OpenAI parallel_tool_calls request flag: case-insensitive model-name substring -> bool, first match wins; empty preserves the provider default (e.g. {"glm-5.1": false})',
    )


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults, description="Default parameters applied to every new agent session")
    aux_models: AuxModelsConfig = Field(
        default_factory=AuxModelsConfig,
        validation_alias=AliasChoices("auxModels", "aux_models"),
        description="Optional auxiliary models for capability bridges, used only when the primary model lacks the relevant modality (e.g. vision); unset fields disable the bridge",
    )


class ModelEntry(Base):
    """Per-model params under a provider. Empty fields fall back to the catalog
    (``provider_models.json``), then to ``agents.defaults`` / the schema default.
    A configured model under its provider is what a ``model_preset`` used to be."""

    max_tokens: int | None = Field(default=None, description="Max output tokens per turn; None falls back to the catalog, then agents.defaults")
    context_window_tokens: int | None = Field(default=None, description="Context window size hint in tokens; None falls back to the catalog, then agents.defaults")
    temperature: float | None = Field(default=None, description="Generation temperature; None falls back to agents.defaults")
    reasoning_effort: str | None = Field(default=None, description="LLM thinking effort (low/medium/high/adaptive/none); None preserves the provider default")
    request_timeout_s: float | None = Field(default=None, description="Per-model HTTP timeout in seconds; overrides DURIN_OPENAI_COMPAT_TIMEOUT_S")
    top_p: float | None = Field(default=None, description="Nucleus sampling (standard OpenAI param); None = don't send")
    top_k: int | None = Field(default=None, description="Top-k sampling; non-standard, sent via extra_body (ollama / LM Studio)")
    repeat_penalty: float | None = Field(default=None, description="Repetition penalty; non-standard, sent via extra_body")


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str | None = Field(default=None, description="API key; prefer ${secret:NAME} references over plaintext")
    api_base: str | None = Field(default=None, description="Custom base URL (local models, proxies, corporate endpoints); None uses the provider default")
    extra_headers: dict[str, str] | None = Field(default=None, description="Custom request headers (e.g. APP-Code for AiHubMix)")
    extra_body: dict[str, Any] | None = Field(default=None, description="Extra fields merged into every request body")
    models: dict[str, ModelEntry] = Field(default_factory=dict, description="Configured models under this provider, with per-model parameter overrides")


class BedrockProviderConfig(ProviderConfig):
    """AWS Bedrock Runtime provider configuration."""

    region: str | None = Field(default=None, description="AWS region; None falls back to AWS_REGION / AWS_DEFAULT_REGION / the profile")
    profile: str | None = Field(default=None, description="Optional AWS shared-config profile name")


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig, description="Any OpenAI-compatible endpoint")
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig, description="Azure OpenAI (model = deployment name)")
    bedrock: BedrockProviderConfig = Field(default_factory=BedrockProviderConfig, description="AWS Bedrock Converse")
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig, description="Anthropic")
    openai: ProviderConfig = Field(default_factory=ProviderConfig, description="OpenAI")
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig, description="OpenRouter API gateway")
    huggingface: ProviderConfig = Field(default_factory=ProviderConfig, description="Hugging Face inference")
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig, description="DeepSeek")
    groq: ProviderConfig = Field(default_factory=ProviderConfig, description="Groq")
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig, description="Zhipu AI")
    zai_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig, description="Z.ai Coding Plan (separate quota from zhipu)")
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig, description="Alibaba DashScope")
    vllm: ProviderConfig = Field(default_factory=ProviderConfig, description="vLLM local server")
    ollama: ProviderConfig = Field(default_factory=ProviderConfig, description="Ollama local models")
    lm_studio: ProviderConfig = Field(default_factory=ProviderConfig, description="LM Studio local models")
    atomic_chat: ProviderConfig = Field(default_factory=ProviderConfig, description="Atomic Chat local models")
    ovms: ProviderConfig = Field(default_factory=ProviderConfig, description="OpenVINO Model Server (OVMS)")
    gemini: ProviderConfig = Field(default_factory=ProviderConfig, description="Google Gemini")
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig, description="Moonshot AI")
    minimax: ProviderConfig = Field(default_factory=ProviderConfig, description="MiniMax")
    minimax_anthropic: ProviderConfig = Field(default_factory=ProviderConfig, description="MiniMax Anthropic-compatible endpoint (thinking)")
    mistral: ProviderConfig = Field(default_factory=ProviderConfig, description="Mistral AI")
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig, description="Step Fun (阶跃星辰)")
    xiaomi_mimo: ProviderConfig = Field(default_factory=ProviderConfig, description="Xiaomi MIMO (小米)")
    longcat: ProviderConfig = Field(default_factory=ProviderConfig, description="LongCat")
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig, description="AiHubMix API gateway")
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig, description="SiliconFlow (硅基流动)")
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig, description="VolcEngine (火山引擎)")
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig, description="VolcEngine Coding Plan")
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig, description="BytePlus (VolcEngine international)")
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig, description="BytePlus Coding Plan")
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True, description="OpenAI Codex (OAuth; managed by `durin login`, not persisted)")
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig, exclude=True, description="GitHub Copilot (OAuth; managed by `durin login`, not persisted)")
    qianfan: ProviderConfig = Field(default_factory=ProviderConfig, description="Baidu Qianfan (百度千帆)")
    nvidia: ProviderConfig = Field(default_factory=ProviderConfig, description="NVIDIA NIM (nvapi- keys)")


class ApiConfig(Base):
    """OpenAI-compatible API server configuration."""

    host: str = Field(default="127.0.0.1", description="Bind address; local-only by default")
    port: int = Field(default=8900, description="API server listen port")
    timeout: float = Field(default=120.0, description="Per-request timeout in seconds")


class CronConfig(Base):
    """Cron scheduler configuration."""

    run_history_max: int = Field(default=50, ge=1, le=1000, description="Maximum run-history entries kept per cron job")
    run_session_retention_hours: int = Field(default=48, ge=0, le=8760, description="Hours a cron run's session data is retained; 0 deletes immediately after the run")


class WorkflowConfig(Base):
    """Workflow engine configuration."""

    max_node_visits: int = Field(default=25, ge=1, description="Cap on total node visits per workflow run, bounding loops")
    keep_runs: int = Field(default=20, ge=1, description="How many recent runs' working folders (.workflow/<run_id>/) to keep on disk")


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = Field(default="127.0.0.1", description="Bind address; local-only by default")
    port: int = Field(default=18790, description="Gateway listen port")
    # Opt-in because the foreground mode is easier to debug on first install.
    daemon: bool = Field(default=False, description="Run `durin gateway` detached (PID file + log file) so the terminal isn't locked")
    # Defaults to True because most users running `durin gateway` want the
    # dashboard — toggling this off skips the auto-enable without touching
    # channels.websocket.
    webui_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("webuiEnabled", "webui_enabled"),
        serialization_alias="webuiEnabled",
        description="Auto-enable the websocket channel at runtime so the embedded web dashboard is served",
    )


class MCPOAuthConfig(Base):
    """OAuth settings for a remote MCP server.

    Presence of an ``oauth`` value (``True`` or this object) marks the server
    as OAuth-requiring. The SDK does dynamic client registration automatically;
    ``client_id`` / ``client_secret`` are an optional static-registration
    override. ``scope`` is an optional requested-scope hint.
    """

    scope: str | None = Field(default=None, description="Optional requested-scope hint sent in the OAuth flow")
    client_id: str | None = Field(default=None, description="Static client registration id (skips dynamic client registration)")
    client_secret: str | None = Field(default=None, description="Static client registration secret; pairs with client_id")
    callback_port: int = Field(default=1456, description="Loopback callback port for `durin mcp login`")


class MCPSamplingConfig(Base):
    """Governance for server-initiated ``sampling/createMessage`` (SP-6).

    Sampling lets an MCP server ask durin's LLM to generate text. It is
    **off by default** — a server only gains LLM access when the user opts
    in. All limits below bound what a server can do once enabled.
    """

    enabled: bool = Field(default=False, description="Allow this MCP server to initiate LLM calls (sampling); off by default")
    model: str | None = Field(default=None, description="Model used for sampling requests; None = the current default model")
    allowed_models: list[str] = Field(default_factory=list, description="Allowlist of models the server may request; empty = no restriction")
    max_tokens_cap: int = Field(default=4096, description="Hard cap on tokens per sampling request")
    requests_per_minute: int = Field(default=10, description="Rate limit for sampling requests")
    allow_tools: bool = Field(default=True, description="Allow tool use inside sampling responses")
    max_tool_rounds: int = Field(default=4, description="Maximum tool-use rounds per sampling request")


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    enabled: bool = Field(default=True, description="Server-level on/off; disabled servers are skipped at connect and can be toggled at runtime")
    type: Literal["stdio", "sse", "streamableHttp"] | None = Field(default=None, description='Transport: "stdio", "sse", or "streamableHttp"; auto-detected when omitted')
    command: str = Field(default="", description='Stdio: command to run (e.g. "npx")')
    args: list[str] = Field(default_factory=list, description="Stdio: command arguments")
    env: dict[str, str] = Field(default_factory=dict, description="Stdio: extra environment variables for the server process")
    url: str = Field(default="", description="HTTP/SSE: endpoint URL")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP/SSE: custom request headers")
    tool_timeout: int = Field(default=30, description="Seconds before a tool call is cancelled")
    tool_timeouts: dict[str, int] = Field(default_factory=dict, description="Per-tool read-timeout override (raw tool name -> seconds)")
    catalog_timeout: float = Field(default=1.5, description="tools/list timeout in seconds at connect, so a hung server can't stall startup")
    keepalive_interval: float = Field(default=180.0, description="Seconds between idle keepalive heartbeats")
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"], description='Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all tools, [] = no tools')
    oauth: bool | MCPOAuthConfig | None = Field(default=None, description="Mark the server as OAuth-requiring; true = dynamic-client-registration defaults, or an object for static registration")
    allow_private_url: bool = Field(default=False, description="Opt this server out of the SSRF private-IP block (false = private IPs blocked)")
    spawn_egress_policy: Literal["warn", "refuse", "off"] = Field(default="warn", description='Stdio: action when the spawn command looks like a shell interpreter with an egress tool: "warn", "refuse", or "off"')
    malware_check: bool = Field(default=True, description="Stdio: query the OSV API for MAL-* advisories before spawning; fail-open on network error")
    sampling: MCPSamplingConfig = Field(default_factory=MCPSamplingConfig, description="Governance for server-initiated LLM sampling")
    version: str = Field(default="", description='Pinned package version from the registry server.json; "" = unpinned')
    source_ref: str = Field(default="", description="Registry ref this server was installed from (drives the update check)")

    def oauth_config(self) -> "MCPOAuthConfig | None":
        """Normalize the oauth field to MCPOAuthConfig | None."""
        if self.oauth is True:
            return MCPOAuthConfig()
        if isinstance(self.oauth, MCPOAuthConfig):
            return self.oauth
        return None


class MCPDeferralConfig(Base):
    """Defer MCP tool definitions behind a discovery bridge (P3, 2026-06-10).

    When the aggregate schema size of registered MCP tools crosses
    ``threshold_tokens``, their definitions stop shipping to the LLM;
    two bridge tools (``mcp_find_tools`` / ``mcp_invoke``) take their
    place. Built-in tools are never deferred. Below the threshold
    everything registers as before — one small server doesn't pay the
    discovery indirection.
    """

    enabled: bool = Field(default=True, description="Enable the MCP tool-deferral bridge when the aggregate tool-schema size crosses the threshold")
    threshold_tokens: int = Field(default=20_000, description="Aggregate MCP tool-schema size in tokens above which definitions are deferred behind mcp_find_tools / mcp_invoke (~10% of a 200k context window)")


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

    web: WebToolsConfig = Field(default_factory=lambda: _lazy_default("durin.agent.tools.web", "WebToolsConfig"), description="Web search and fetch tools")
    exec: ExecToolConfig = Field(default_factory=lambda: _lazy_default("durin.agent.tools.shell", "ExecToolConfig"), description="Shell execution tool")
    my: MyToolConfig = Field(default_factory=lambda: _lazy_default("durin.agent.tools.self", "MyToolConfig"), description="Self-inspection tool")
    post_edit_check: PostEditCheckConfig = Field(default_factory=lambda: _lazy_default("durin.agent.tools.post_edit_check", "PostEditCheckConfig"), description="Post-edit linter checks on written/edited files")
    code_execution: CodeExecutionConfig = Field(default_factory=lambda: _lazy_default("durin.agent.tools.code_execution", "CodeExecutionConfig"), description="execute_code sandboxed Python tool")
    process: ProcessToolConfig = Field(default_factory=lambda: _lazy_default("durin.agent.tools.process_registry", "ProcessToolConfig"), description="Background-process registry (exec background=true / process tool)")
    restrict_to_workspace: bool = Field(default=False, description="Restrict all tool file access to the workspace directory")
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict, description="MCP server connections, keyed by server name")
    mcp_discovery: McpDiscoveryConfig = Field(default_factory=McpDiscoveryConfig, description="MCP server discovery: registries, result cap, install gate")
    mcp_deferral: MCPDeferralConfig = Field(default_factory=MCPDeferralConfig, description="Defer MCP tool definitions behind a discovery bridge when their aggregate schema size is large")
    ssrf_whitelist: list[str] = Field(default_factory=list, description='CIDR ranges to exempt from SSRF blocking (e.g. ["100.64.0.0/10"] for Tailscale)')


class InstallConfig(Base):
    """Persistent install-level state.

    ``extras`` is the set of optional dependency extras the user has had
    at any point. It's *additive*: durin appends here whenever it detects
    a new importable extra, but never removes entries automatically.
    That way `pipx uninstall` + `pipx install` (which drops extras) gets
    flagged by `durin doctor` instead of silently forgotten.
    """

    extras: list[str] = Field(default_factory=list, description="Additive set of optional dependency extras ever detected as importable; never auto-removed, so `durin doctor` can flag a reinstall that dropped them")
    auto_install_extras: bool = Field(
        default=True,
        description=(
            "Auto-install a feature's pip extra when it's activated (frictionless). "
            "Off falls back to a 'pip install durin-agent[X]' message."
        ),
    )


class AppearanceConfig(Base):
    """Visual theme — shared by the TUI and the web dashboard.

    Two axes (see ``design/DESIGN.md``): ``palette`` is the colour
    identity, ``mode`` is light/dark. ``mode = "auto"`` detects the
    terminal (``COLORFGBG``) or the browser's ``prefers-color-scheme``.
    """

    palette: str = Field(default="ithildin", description="Colour identity: ithildin, forge, or mithril")
    mode: str = Field(default="auto", description="Light/dark mode: auto (detect from terminal/browser), light, or dark")


class TelemetryPushConfig(Base):
    """Opt-in HTTPS push of telemetry events.

    Default OFF. When enabled, every event emitted locally also POSTs
    to ``url`` (buffered, batched per ``batch_size``). The local JSONL
    persistence under ``~/.cache/durin/telemetry/`` runs UNCHANGED —
    push is an ADDITIONAL sink, never a replacement.

    **Privacy**: events carry truncated user content (queries,
    snippets, needles — 200 chars max via ``_truncate_freetext`` in
    ``durin/agent/tools/_telemetry.py``). Enable this only when
    exporting to YOUR OWN infrastructure (Grafana/Loki/Datadog/custom
    endpoint).

    **Auth**: ``token_secret_name`` references a secret stored in
    ``~/.durin/secrets.json`` — NEVER put the bearer token directly
    in ``config.json``. Use ``durin secrets set <name> <token>``.
    """

    enabled: bool = Field(default=False, description="Enable HTTPS push of telemetry events; local JSONL persistence runs unchanged either way")
    url: str | None = Field(default=None, description="Destination endpoint URL for the telemetry POSTs")
    token_secret_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "tokenSecretName", "token_secret_name",
        ),
        description="Durin secret name holding the bearer token (use `durin secret set <name> <token>`; never put the token in config directly)",
    )
    batch_size: int = Field(default=10, ge=1, le=1000, description="Events per HTTP batch")


class TelemetryConfig(Base):
    """Telemetry subsystem configuration.

    Events emit locally to JSONL by default; optional fan-out to an
    HTTPS endpoint via :class:`TelemetryPushConfig`.
    """

    push: TelemetryPushConfig = Field(default_factory=TelemetryPushConfig, description="Opt-in HTTPS fan-out of telemetry events to your own endpoint")


class LoggingConfig(Base):
    """Gateway daemon log lifecycle (web-editable).

    Governs ONLY the gateway's ``gateway.log`` file sink — rotation by
    size, gz compression of rotated segments, deletion by age. The
    telemetry subsystem has its own independent lifecycle
    (``durin/telemetry/retention.py``) and is NOT affected by these keys.
    """

    max_file_mb: int = Field(
        default=5, ge=1, le=1024,
        validation_alias=AliasChoices("maxFileMb", "max_file_mb"),
        serialization_alias="maxFileMb",
        description="File size in MB at which gateway.log rotates to a new segment",
    )
    retention_days: int = Field(
        default=7, ge=1, le=365,
        validation_alias=AliasChoices("retentionDays", "retention_days"),
        serialization_alias="retentionDays",
        description="Age in days at which rotated gateway log segments are deleted",
    )


class CatalogRefreshConfig(Base):
    """Daily models.dev catalog refresh into a user-cache overlay.

    A top-level section (not under ``providers``) — it is not a provider, and
    nesting it in the providers dict would entangle it with the provider-section
    prune/iteration logic.
    """

    enabled: bool = Field(default=True, description="Enable the periodic models.dev catalog refresh")
    interval_hours: int = Field(
        default=24, ge=1,
        validation_alias=AliasChoices("intervalHours", "interval_hours"),
        serialization_alias="intervalHours",
        description="Refresh interval in hours",
    )


class McpCatalogRefreshConfig(Base):
    """Periodic refresh of the durin-owned MCP catalog from a raw GitHub URL.

    A top-level section (not under ``tools``) — mirrors the shape of
    ``CatalogRefreshConfig`` for the skills model catalog.
    """

    enabled: bool = Field(default=True, description="Enable the periodic MCP catalog refresh")
    # Published weekly as a release asset (see .github/workflows/mcp-catalog.yml) —
    # a release asset, not a committed file, so the weekly rebuild never bloats git history.
    url: str = Field(
        default="https://github.com/mmarmol/durin/releases/download/catalog/mcp_catalog.json",
        description="Catalog download URL (a weekly-published release asset)",
    )
    interval_hours: int = Field(
        default=168, ge=1,
        validation_alias=AliasChoices("intervalHours", "interval_hours"),
        serialization_alias="intervalHours",
        description="Refresh interval in hours (default 7 days)",
    )


class Config(BaseSettings):
    """Root configuration for durin."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig, description="Agent loop: active model, generation parameters, session behavior, concurrency caps, auxiliary models")
    appearance: AppearanceConfig = Field(default_factory=AppearanceConfig, description="Visual theme (palette + light/dark mode) shared by the TUI and the web dashboard")
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig, description="Global chat-channel defaults; channel-specific config lives as extra keys under this section")
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig, description="Global voice transcription settings; channel-level keys override per-channel")
    tts: TtsConfig = Field(default_factory=TtsConfig, description="Text-to-speech for spoken replies in conversational voice mode")
    voice: VoiceConfig = Field(default_factory=VoiceConfig, description="Hands-free conversational voice mode (the gateway loop)")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Memory subsystem: vector retrieval, dream passes, file watcher, health checks")
    cron: CronConfig = Field(default_factory=CronConfig, description="Cron scheduler: run history and per-run session retention")
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig, description="Workflow engine: node-visit caps and run-folder retention")
    skills: SkillsConfig = Field(default_factory=SkillsConfig, description="Skill subsystem governance: import security, install policy, discovery registries")
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig, description="API credentials and per-model parameter overrides for every LLM provider")
    catalog_refresh: CatalogRefreshConfig = Field(
        default_factory=CatalogRefreshConfig,
        validation_alias=AliasChoices("catalogRefresh", "catalog_refresh"),
        serialization_alias="catalogRefresh",
        description="Daily models.dev catalog refresh into a user-cache overlay",
    )
    mcp_catalog_refresh: McpCatalogRefreshConfig = Field(
        default_factory=McpCatalogRefreshConfig,
        validation_alias=AliasChoices("mcpCatalogRefresh", "mcp_catalog_refresh"),
        serialization_alias="mcpCatalogRefresh",
        description="Periodic refresh of the durin-owned MCP catalog",
    )
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig, description="Telemetry: local JSONL always on, optional HTTPS push fan-out")
    logging: LoggingConfig = Field(default_factory=LoggingConfig, description="Gateway daemon log lifecycle: rotation size and retention age for gateway.log")
    api: ApiConfig = Field(default_factory=ApiConfig, description="OpenAI-compatible API server: bind address, port, timeout")
    gateway: GatewayConfig = Field(default_factory=GatewayConfig, description="Gateway/server: bind address, port, daemon mode, embedded web dashboard")
    tools: ToolsConfig = Field(default_factory=ToolsConfig, description="Built-in agent tools and MCP server connections")
    install: InstallConfig = Field(default_factory=InstallConfig, description="Persistent install-level state (managed by durin; rarely edited by hand)")
    model_presets: dict[str, ModelPresetConfig] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("modelPresets", "model_presets"),
        description="Named sets of model + generation parameters for quick switching, keyed by preset name",
    )
    personas: dict[str, PersonaConfig] = Field(default_factory=dict, description="Named personas (a SOUL plus an optional model), keyed by persona name")
    # The built-ins (build/plan/explore) are not stored here; they cannot be
    # overridden by a config entry.
    agent_modes: dict[str, ModeConfig] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("agentModes", "agent_modes"),
        description="User-defined agent modes, keyed by name, registered at startup alongside the built-ins",
    )
    model_capabilities: dict[str, ModelCapabilityOverride] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("modelCapabilities", "model_capabilities"),
        description='Per-model capability overrides keyed by bare model name ("glm-5-turbo") or provider-qualified form ("custom/glm-5-turbo"); provider-qualified keys win; values override the vendored snapshot for that model only',
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

    def _resolve_model_params(self, provider: str, model: str):
        """Return ``(entry, caps)`` for a ``(provider, model)``: the user's
        ``ModelEntry`` override (if any) and the catalog capabilities (if any).

        Codex models inherit the matching ``openai`` caps via
        ``provider_models('openai_codex')``; the lookup uses the static codex
        slug fallback (no token → no network), so it is safe to consult here.
        """
        entry = None
        caps = None
        if provider and provider != "auto":
            pc = getattr(self.providers, provider, None)
            if pc is not None:
                entry = (getattr(pc, "models", None) or {}).get(model)
            try:
                from durin.providers.provider_catalog import catalog_model_caps

                caps = catalog_model_caps(provider, model)
            except Exception:  # noqa: BLE001
                caps = None
        return entry, caps

    def resolve_default_preset(self) -> ModelPresetConfig:
        """The implicit `default` preset: provider.models → catalog → defaults."""
        d = self.agents.defaults
        entry, caps = self._resolve_model_params(d.provider, d.model)
        ctx = (
            entry.context_window_tokens
            if entry and entry.context_window_tokens is not None
            else (caps.max_input_tokens if caps and caps.max_input_tokens else d.context_window_tokens)
        )
        mt = (
            entry.max_tokens
            if entry and entry.max_tokens is not None
            else (caps.max_output_tokens if caps and caps.max_output_tokens else d.max_tokens)
        )
        temp = entry.temperature if entry and entry.temperature is not None else d.temperature
        eff = entry.reasoning_effort if entry and entry.reasoning_effort is not None else d.reasoning_effort
        timeout = entry.request_timeout_s if entry and entry.request_timeout_s is not None else None
        top_p = entry.top_p if entry and entry.top_p is not None else None
        top_k = entry.top_k if entry and entry.top_k is not None else None
        repeat_penalty = entry.repeat_penalty if entry and entry.repeat_penalty is not None else None
        return ModelPresetConfig(
            model=d.model, provider=d.provider, max_tokens=mt,
            context_window_tokens=ctx, temperature=temp, reasoning_effort=eff,
            request_timeout_s=timeout,
            top_p=top_p, top_k=top_k, repeat_penalty=repeat_penalty,
        )

    def resolve_preset(self, name: str | None = None) -> ModelPresetConfig:
        """Return effective model params from a named preset or the implicit default."""
        name = self.agents.defaults.model_preset if name is None else name
        if not name or name == "default":
            return self.resolve_default_preset()
        if name not in self.model_presets:
            raise KeyError(f"model_preset {name!r} not found in model_presets")
        return self.model_presets[name]

    def resolve_persona(self, name: str | None = None) -> "PersonaConfig | None":
        """Resolve a persona by name from user config. ``None`` when the name is
        unset/empty or unknown (caller falls back to the default SOUL + default
        model)."""
        if name is None:
            name = self.agents.defaults.persona
        if not name:
            return None
        return self.personas.get(name)

    def persona_names(self) -> list[str]:
        return sorted(self.personas)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def match_provider_by_name(
        self, model: str,
    ) -> tuple["ProviderConfig | None", str | None]:
        """Name-based provider detection ONLY: explicit provider prefix, then
        registry keywords, over configured providers. No last-resort fallback —
        a name nothing recognizably serves returns ``(None, None)``. This is the
        placement check for specific-model knobs (aux/judge), where guessing a
        provider sends the name to the wrong endpoint."""
        from durin.providers.registry import PROVIDERS

        model_lower = model.lower()
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
        return None, None

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

        by_name = self.match_provider_by_name(model or resolved.model)
        if by_name[1]:
            return by_name

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

    from durin.agent.tools.code_execution import CodeExecutionConfig
    from durin.agent.tools.post_edit_check import PostEditCheckConfig
    from durin.agent.tools.process_registry import ProcessToolConfig
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
    mod.PostEditCheckConfig = PostEditCheckConfig  # type: ignore[attr-defined]
    mod.CodeExecutionConfig = CodeExecutionConfig  # type: ignore[attr-defined]
    mod.ProcessToolConfig = ProcessToolConfig  # type: ignore[attr-defined]

    ToolsConfig.model_rebuild()
    Config.model_rebuild()


# Eagerly resolve when the import chain allows it (no circular deps at this
# point).  If it fails (first import triggers a cycle), the rebuild will
# happen lazily when Config/ToolsConfig is first used at runtime.
try:
    _resolve_tool_config_refs()
except ImportError:
    pass
