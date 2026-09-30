"""Create LLM providers from config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from durin.config.schema import Config, InlineFallbackConfig, ModelPresetConfig
from durin.providers.base import LLMProvider
from durin.providers.fallback_provider import FallbackProvider
from durin.providers.registry import find_by_name


def _resolve_secret_refs(obj: Any) -> Any:
    """Recursively resolve ``${secret:}`` references in a config value.

    Provider ``extra_headers`` / ``extra_body`` may carry a credential in a
    custom header or body field; resolve those refs the same way ``api_key`` is
    resolved, so the plaintext (not the literal ref) reaches the wire. Literals,
    ``None`` and non-string scalars pass through untouched.
    """
    from durin.security.secrets import resolve_secret

    if isinstance(obj, dict):
        return {k: _resolve_secret_refs(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_secret_refs(v) for v in obj]
    return resolve_secret(obj)


@dataclass(frozen=True)
class ProviderSnapshot:
    provider: LLMProvider
    model: str
    context_window_tokens: int
    signature: tuple[object, ...]
    # Tier 2 A1: per-preset pre-emptive compaction trigger. ``None`` means
    # "inherit the global default from AgentDefaults". Resolved here at
    # snapshot time so a preset switch propagates the new ratio into the
    # consolidator without the loop having to know about preset internals.
    preemptive_compact_ratio: float | None = None


def _resolve_model_preset(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> ModelPresetConfig:
    """The preset a provider is built from, with concrete limits: *preset*
    when one is given (whatever it leaves unset takes its model's own
    limits), else the named or the active preset."""
    if preset is not None:
        return config.resolve_preset_limits(preset)
    return config.resolve_preset(preset_name)


def _make_provider_core(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    model: str | None = None,
) -> LLMProvider:
    """Create a plain LLM provider without failover wrapping."""
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    return _build_provider(config, resolved, model=model)


def _build_provider(
    config: Config,
    resolved: ModelPresetConfig,
    *,
    model: str | None = None,
) -> LLMProvider:
    """Create a plain LLM provider from a preset whose limits are resolved."""
    model = model or resolved.model
    provider_name = config.get_provider_name(model, preset=resolved)
    p = config.get_provider(model, preset=resolved)
    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"

    # Resolve a `${secret:NAME}` reference once; a literal passes
    # through. Done here so the plaintext key never lands back in the
    # `Config` object — only this local and the provider client hold it.
    from durin.security.secrets import resolve_secret

    api_key = resolve_secret(p.api_key) if p else None
    extra_headers = _resolve_secret_refs(p.extra_headers) if p else None
    extra_body = _resolve_secret_refs(p.extra_body) if p else None

    if backend == "azure_openai":
        if not api_key or not (p and p.api_base):
            raise ValueError("Azure OpenAI requires api_key and api_base in config.")
    elif backend == "openai_compat" and not model.startswith("bedrock/"):
        needs_key = not api_key
        exempt = spec and (spec.is_oauth or spec.is_local or spec.is_direct)
        if needs_key and not exempt:
            raise ValueError(f"No API key configured for provider '{provider_name}'.")

    if backend == "openai_codex":
        from durin.providers.openai_codex_provider import OpenAICodexProvider

        provider = OpenAICodexProvider(default_model=model)
    elif backend == "azure_openai":
        from durin.providers.azure_openai_provider import AzureOpenAIProvider

        provider = AzureOpenAIProvider(
            api_key=api_key,
            api_base=p.api_base,
            default_model=model,
        )
    elif backend == "github_copilot":
        from durin.providers.github_copilot_provider import GitHubCopilotProvider

        provider = GitHubCopilotProvider(default_model=model)
    elif backend == "anthropic":
        from durin.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(
            api_key=api_key,
            api_base=config.get_api_base(model, preset=resolved),
            default_model=model,
            extra_headers=extra_headers,
        )
    elif backend == "bedrock":
        from durin.providers.bedrock_provider import BedrockProvider

        provider = BedrockProvider(
            api_key=api_key,
            api_base=p.api_base if p else None,
            default_model=model,
            region=getattr(p, "region", None) if p else None,
            profile=getattr(p, "profile", None) if p else None,
            extra_body=extra_body,
        )
    else:
        from durin.providers.openai_compat_provider import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key=api_key,
            api_base=config.get_api_base(model, preset=resolved),
            default_model=model,
            extra_headers=extra_headers,
            spec=spec,
            extra_body=extra_body,
            parallel_tool_calls_overrides=dict(config.agents.defaults.parallel_tool_calls),
            request_timeout_s=resolved.request_timeout_s,
        )

    provider.generation = resolved.to_generation_settings()
    provider.provider_key = provider_name or None
    return provider


def _inline_fallback_preset(
    primary: ModelPresetConfig,
    fallback: InlineFallbackConfig,
) -> ModelPresetConfig:
    """An inline fallback as a preset. Its limits are its own model's (the
    same chain as any preset), never the primary's: a failover to a model
    with a smaller window must not be sized by the primary's."""
    return ModelPresetConfig(
        model=fallback.model,
        provider=fallback.provider,
        max_tokens=fallback.max_tokens,
        context_window_tokens=fallback.context_window_tokens,
        temperature=(
            fallback.temperature if fallback.temperature is not None else primary.temperature
        ),
        reasoning_effort=fallback.reasoning_effort,
    )


class _Fallback(NamedTuple):
    preset: ModelPresetConfig  # limits resolved
    window_known: bool
    label: str  # the preset name, or where the inline fallback sits


def _resolve_fallbacks(config: Config, primary: ModelPresetConfig) -> list[_Fallback]:
    out: list[_Fallback] = []
    for index, fallback in enumerate(config.agents.defaults.fallback_models):
        if isinstance(fallback, str):
            resolved, known = config.resolve_limits(config.model_presets[fallback])
            out.append(_Fallback(resolved, known, fallback))
        else:
            resolved, known = config.resolve_limits(_inline_fallback_preset(primary, fallback))
            out.append(_Fallback(resolved, known, f"agents.defaults.fallback_models.{index}"))
    return out


def _resolve_fallback_presets(config: Config, primary: ModelPresetConfig) -> list[ModelPresetConfig]:
    return [fallback.preset for fallback in _resolve_fallbacks(config, primary)]


def make_provider(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    model: str | None = None,
) -> LLMProvider:
    """Create the LLM provider implied by config.

    When *model* is given, it overrides the resolved/preset model — used by
    the failover path to create providers for fallback models.
    """
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    provider = _build_provider(config, resolved, model=model)
    fallback_presets = _resolve_fallback_presets(config, resolved)

    if fallback_presets:
        provider = FallbackProvider(
            primary=provider,
            fallback_presets=fallback_presets,
            provider_factory=lambda fb: _build_provider(config, fb),
        )

    return provider


def provider_signature(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> tuple[object, ...]:
    """Return the config fields that affect the active provider chain."""
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    return _signature(config, resolved, _resolve_fallback_presets(config, resolved))


class CappingFallback(NamedTuple):
    """The fallback whose window is the one a run gets (smaller than the
    run's own model's)."""

    label: str  # the preset name, or agents.defaults.fallback_models.<i>
    provider: str  # the provider it runs on
    model: str
    context_window_tokens: int


def _signature(
    config: Config,
    resolved: ModelPresetConfig,
    fallback_presets: list[ModelPresetConfig],
) -> tuple[object, ...]:
    p = config.get_provider(resolved.model, preset=resolved)

    def _fallback_signature(fallback: ModelPresetConfig) -> tuple[object, ...]:
        fp = config.get_provider(fallback.model, preset=fallback)
        return (
            fallback.model,
            fallback.provider,
            config.get_provider_name(fallback.model, preset=fallback),
            config.get_api_key(fallback.model, preset=fallback),
            config.get_api_base(fallback.model, preset=fallback),
            fp.extra_headers if fp else None,
            fp.extra_body if fp else None,
            getattr(fp, "region", None) if fp else None,
            getattr(fp, "profile", None) if fp else None,
            fallback.max_tokens,
            fallback.temperature,
            fallback.reasoning_effort,
            fallback.context_window_tokens,
        )

    return (
        resolved.model,
        resolved.provider,
        config.get_provider_name(resolved.model, preset=resolved),
        config.get_api_key(resolved.model, preset=resolved),
        config.get_api_base(resolved.model, preset=resolved),
        p.extra_headers if p else None,
        p.extra_body if p else None,
        getattr(p, "region", None) if p else None,
        getattr(p, "profile", None) if p else None,
        resolved.max_tokens,
        resolved.temperature,
        resolved.reasoning_effort,
        resolved.context_window_tokens,
        tuple(_fallback_signature(fallback) for fallback in fallback_presets),
    )


def preset_context_window(config: Config, preset: ModelPresetConfig) -> int:
    """The context window a run on *preset* gets: the preset's own (its
    model's when the preset sets none), capped by every fallback model's,
    since a failover must fit the same prompt."""
    return preset_window_cap(config, preset)[0]


def preset_window_cap(
    config: Config, preset: ModelPresetConfig,
) -> tuple[int, CappingFallback | None]:
    """``preset_context_window`` and the fallback that sets it, when one
    does."""
    resolved = config.resolve_preset_limits(preset)
    return _window_and_cap(config, resolved, _resolve_fallbacks(config, resolved))


def _window_and_cap(
    config: Config, resolved: ModelPresetConfig, fallbacks: list[_Fallback],
) -> tuple[int, CappingFallback | None]:
    """The run's own window, lowered to any smaller fallback window that is
    known. A fallback whose window would only be agents.defaults' guess (a
    model no entry or catalog describes) does not lower it: that guess would
    shrink a known window — a 1M chat to 65,536 — on no evidence."""
    window, capping = resolved.context_window_tokens, None
    for fallback in fallbacks:
        if fallback.window_known and fallback.preset.context_window_tokens < window:
            window = fallback.preset.context_window_tokens
            capping = CappingFallback(
                fallback.label,
                config.routed_provider(fallback.preset.provider, fallback.preset.model),
                fallback.preset.model,
                window,
            )
    return window, capping


def build_provider_snapshot(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> ProviderSnapshot:
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    fallbacks = _resolve_fallbacks(config, resolved)
    return ProviderSnapshot(
        provider=make_provider(config, preset=resolved),
        model=resolved.model,
        context_window_tokens=_window_and_cap(config, resolved, fallbacks)[0],
        signature=_signature(config, resolved, [fallback.preset for fallback in fallbacks]),
        preemptive_compact_ratio=resolved.preemptive_compact_ratio,
    )


def load_provider_snapshot(
    config_path: Path | None = None,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> ProviderSnapshot:
    from durin.config.loader import load_config, resolve_config_env_vars

    return build_provider_snapshot(
        resolve_config_env_vars(load_config(config_path)),
        preset_name=preset_name,
        preset=preset,
    )


def load_default_preset(config_path: Path | None = None) -> ModelPresetConfig:
    """Resolve the implicit ``default`` preset from the live on-disk config.

    The runtime captures ``model_presets["default"]`` once at construction; the
    daemon refresh uses this to re-read it so a default-model change in Settings
    is reflected without a restart.
    """
    from durin.config.loader import load_config, resolve_config_env_vars

    return resolve_config_env_vars(load_config(config_path)).resolve_default_preset()
