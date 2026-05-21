"""Task-oriented onboarding wizard.

The legacy ``onboard.py`` walks the Pydantic schema field-by-field.
That's exhaustive but exhausting — users get drowned in choices for
settings they don't care about, while the *important* questions
(provider, key, default model, which optional features) are buried.

This wizard flips that:

1. **Required stage** — Provider, API key, default model. Without
   these, durin can't talk to any LLM, so we don't let the user out
   of the wizard until they're set.
2. **Optional menu** — Checklist of capabilities (memory, vision,
   audio, image gen, web search, channels). User picks what to
   configure now; everything else can be added later via
   `durin config set ...` or a future `durin onboard --add <feature>`.
3. **Review** — Summary of what got configured + the exact
   ``pipx inject …`` (or ``pip install``) command to add any extras
   that need separate installation.

Anything beyond that is power-user territory and lives in the legacy
field-walking wizard (kept around as ``onboard --advanced``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from durin.config.schema import (
    AuxModelConfig,
    Config,
    MemoryEmbeddingConfig,
)

__all__ = [
    "WizardResult",
    "run_wizard",
    "PROVIDER_CHOICES",
    "DEFAULT_MODELS",
    "apply_model_capabilities",
]


def apply_model_capabilities(config: Config, model: str, provider: str) -> list[str]:
    """Sync ``agents.defaults`` with the chosen model's known capabilities.

    durin ships a 3-source capability snapshot (litellm + models.dev +
    manual overrides). When the user picks a default model we look that
    model up and apply the values that durin's runtime actually reads —
    today that's ``context_window_tokens`` (drives compaction). The
    schema default (65536) is wrong for most modern models (glm-5.1 is
    ~203K, Claude is 200K, …), so without this step the agent compacts
    far too early.

    ``max_tokens`` (the generation output cap) is intentionally left
    alone — it's a user-tunable budget knob, not a hard model fact.

    Returns a list of human-readable lines describing what changed, for
    the wizard summary.
    """
    from durin.providers.capabilities import get_model_capabilities

    changed: list[str] = []
    caps = get_model_capabilities(model, provider or None)
    win = caps.max_input_tokens
    if isinstance(win, int) and win > 0 and config.agents.defaults.context_window_tokens != win:
        old = config.agents.defaults.context_window_tokens
        config.agents.defaults.context_window_tokens = win
        changed.append(
            f"context window: {old:,} → {win:,} tokens (from {model} capabilities)"
        )
    return changed


# ---------------------------------------------------------------------------
# Static catalogues
# ---------------------------------------------------------------------------


# (label, internal provider name, recommended default model)
# The order is the order the user sees in the menu — top entries are
# what we'd recommend for someone who has no preference yet.
PROVIDER_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("Z.AI Coding Plan (recommended)", "zhipu", "glm-5.1"),
    ("Anthropic (Claude)", "anthropic", "claude-opus-4-7"),
    ("OpenAI (GPT)", "openai", "gpt-5"),
    ("Google (Gemini)", "gemini", "gemini-2.5-pro"),
    ("OpenRouter (any model, one key)", "openrouter", "anthropic/claude-opus-4.7"),
    ("Custom OpenAI-compatible endpoint", "custom", ""),
)

# Suggestions shown after the user picks a provider. Plain list — not
# enforced. The user can always type a model name we don't know.
DEFAULT_MODELS: dict[str, tuple[str, ...]] = {
    "zhipu": ("glm-5.1", "glm-5-turbo", "glm-5v-turbo"),
    "anthropic": ("claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"),
    "openai": ("gpt-5", "gpt-5-mini", "gpt-4.1"),
    "gemini": ("gemini-2.5-pro", "gemini-2.5-flash"),
    "openrouter": (
        "anthropic/claude-opus-4.7",
        "openai/gpt-5",
        "google/gemini-2.5-pro",
    ),
    "custom": (),
}

# (label, embedding-provider, embedding-model id, approx download size)
_EMBEDDING_CHOICES: tuple[tuple[str, str, str, str], ...] = (
    ("multilingual-e5-small (default, 130MB, 100+ languages)",
     "fastembed", "intfloat/multilingual-e5-small", "130 MB"),
    ("BGE-M3 (large, 2.2GB, multilingual, strongest)",
     "fastembed", "BAAI/bge-m3", "2.2 GB"),
    ("all-MiniLM-L6-v2 (90MB, English-only, fastest)",
     "fastembed", "sentence-transformers/all-MiniLM-L6-v2", "90 MB"),
)


# ---------------------------------------------------------------------------
# Result + entry point
# ---------------------------------------------------------------------------


@dataclass
class WizardResult:
    """Outcome of one wizard run."""

    config: Config
    extras_to_install: list[str] = field(default_factory=list)
    cancelled: bool = False
    summary_lines: list[str] = field(default_factory=list)


def run_wizard(initial_config: Config, *, q: Any | None = None) -> WizardResult:
    """Run the wizard against ``initial_config`` and return the result.

    ``q`` is an injection point for tests: pass a mock with the same
    surface as :mod:`questionary` to drive the wizard programmatically.
    When unset we import questionary lazily (it's an optional dep).
    """
    if q is None:
        try:
            import questionary as q  # type: ignore[no-redef]
        except ImportError as exc:
            raise RuntimeError(
                "The onboarding wizard needs the 'questionary' package. "
                "Run `pip install questionary` or use `durin onboard --no-wizard` "
                "to just write defaults."
            ) from exc

    config = initial_config.model_copy(deep=True)
    extras: set[str] = set()
    summary: list[str] = []

    # ---- Stage 1: required ------------------------------------------
    if not _stage_provider_and_model(config, q, summary):
        return WizardResult(
            config=initial_config, extras_to_install=[],
            cancelled=True, summary_lines=summary,
        )

    # ---- Stage 2: optional menu -------------------------------------
    _stage_optional_menu(config, extras, q, summary)

    # ---- Stage 3: workspace (one quick question) --------------------
    _stage_workspace(config, q, summary)

    return WizardResult(
        config=config,
        extras_to_install=sorted(extras),
        cancelled=False,
        summary_lines=summary,
    )


# ---------------------------------------------------------------------------
# Stage 1 — Provider + model (required)
# ---------------------------------------------------------------------------


def _provider_is_configured(config: Config) -> bool:
    """True when provider + model + (key or local/oauth) are already set.

    This is the gate for "you don't have to reconfigure" — a re-run of
    `durin onboard` against an existing setup should let the user keep
    what works instead of forcing the whole flow again.
    """
    d = config.agents.defaults
    if d.provider == "auto" or not d.model:
        return False
    provider_obj = getattr(config.providers, d.provider, None)
    if provider_obj is None:
        return False
    # A configured provider has an api_key OR an api_base (local
    # endpoints) — OAuth providers are treated as configured too since
    # their tokens live outside config.json.
    return bool(
        getattr(provider_obj, "api_key", None)
        or getattr(provider_obj, "api_base", None)
        or d.provider in ("openai_codex", "github_copilot")
    )


def _test_configured_model(q: Any) -> None:
    """Run a real round-trip against the on-disk default model + print the result."""
    try:
        from durin.cli.doctor import check_model_ping
    except Exception as e:  # noqa: BLE001
        print(f"  Could not run the model test: {e}")
        return
    print("  Testing the model (a real round-trip)…")
    result = check_model_ping()
    if result.status == "ok":
        print(f"  ✓ {result.message}")
    else:
        print(f"  ✗ {result.message}")
        if result.fix:
            print(f"    {result.fix}")


def _stage_provider_and_model(config: Config, q: Any, summary: list[str]) -> bool:
    """Configure provider + API key + default model. Returns False on cancel.

    If a working setup already exists, the user is NOT forced to
    reconfigure — they can test the model and continue, or choose to
    change it.
    """
    if _provider_is_configured(config):
        d = config.agents.defaults
        while True:
            action = q.select(
                f"Provider already configured: {d.provider} · {d.model}. "
                "What do you want to do?",
                choices=[
                    "Keep it and continue",
                    "Test the model first",
                    "Change provider / model",
                ],
            ).ask()
            if action is None:
                return False
            if action.startswith("Keep"):
                summary.append(f"Provider: {d.provider} ({d.model}) — kept")
                return True
            if action.startswith("Test"):
                _test_configured_model(q)
                continue  # loop back to the keep/test/change menu
            # "Change …" → fall through to the full pick flow below.
            break

    label_to_choice = {label: (name, model) for label, name, model in PROVIDER_CHOICES}
    chosen_label = q.select(
        "Which LLM provider do you want to use?",
        choices=list(label_to_choice.keys()),
    ).ask()
    if chosen_label is None:
        return False
    provider_name, recommended_model = label_to_choice[chosen_label]

    # API key (some providers — local custom endpoints — may not need one).
    api_key = q.password(
        f"Paste your {provider_name} API key (leave blank if not required):"
    ).ask()
    if api_key is None:
        return False
    if api_key:
        _set_provider_api_key(config, provider_name, api_key)

    # Default model — suggestions per provider, plus free-form fallback.
    suggestions = list(DEFAULT_MODELS.get(provider_name, ()))
    if recommended_model and recommended_model not in suggestions:
        suggestions.insert(0, recommended_model)
    if suggestions:
        suggestions = list(dict.fromkeys(suggestions))  # de-dupe, preserve order
        suggestions.append("Other (type below)")
        model_pick = q.select("Default model:", choices=suggestions).ask()
        if model_pick is None:
            return False
        if model_pick == "Other (type below)":
            model_pick = q.text("Model name:").ask()
            if not model_pick:
                return False
    else:
        model_pick = q.text("Model name:").ask()
        if not model_pick:
            return False

    config.agents.defaults.provider = provider_name
    config.agents.defaults.model = model_pick

    summary.append(f"Provider: {provider_name} ({model_pick})")

    # Sync the context window (and any other runtime-relevant caps) from
    # the model's known capability snapshot.
    for line in apply_model_capabilities(config, model_pick, provider_name):
        summary.append(line)
    return True


def _set_provider_api_key(config: Config, provider_name: str, api_key: str) -> None:
    """Write the API key into the right ``providers.<name>.api_key`` slot."""
    providers = config.providers
    provider_obj = getattr(providers, provider_name, None)
    if provider_obj is None:
        # Unknown name — treat as `custom` so the key isn't lost.
        provider_obj = providers.custom
    provider_obj.api_key = api_key


# ---------------------------------------------------------------------------
# Stage 2 — Optional features menu
# ---------------------------------------------------------------------------


_OPTIONAL_FEATURES: tuple[tuple[str, str, str], ...] = (
    ("memory", "📁 Vector memory",
     "Semantic recall across sessions. ~130 MB embedding model on first use."),
    ("vision", "👁️  Vision (interpret_image)",
     "Lets the agent describe images / read screenshots."),
    ("audio", "🎤 Audio transcription (interpret_audio)",
     "Lets the agent transcribe and summarise audio clips."),
    ("image_gen", "🎨 Image generation",
     "DALL-E / generate_image tool."),
    ("web", "🔍 Web search + fetch",
     "web_search and web_fetch tools. Adds the `[web]` extra."),
    ("channels", "💬 Channels (Telegram / Slack / WhatsApp / …)",
     "Run `durin gateway` to bridge with chat platforms."),
)


def _detect_configured_features(config: Config) -> set[str]:
    """Return the optional-feature keys that already look configured.

    Lets the menu show ``[✓]`` for things the user set up on a previous
    run, so re-running `durin onboard` doesn't pretend it's a clean
    install.
    """
    found: set[str] = set()
    aux = getattr(config.agents, "aux_models", None)
    if aux is not None:
        if getattr(aux, "vision", None) is not None:
            found.add("vision")
        if getattr(aux, "audio", None) is not None:
            found.add("audio")
    # memory: a non-default embedding model means the user picked one.
    try:
        from durin.config.schema import MemoryEmbeddingConfig

        if config.memory.embedding.model != MemoryEmbeddingConfig().model:
            found.add("memory")
    except Exception:  # noqa: BLE001
        pass
    if getattr(config.tools.web, "enable", False):
        found.add("web")
    if getattr(config.gateway, "webui_enabled", False) or getattr(
        config.gateway, "daemon", False
    ):
        found.add("channels")
    return found


def _stage_optional_menu(
    config: Config, extras: set[str], q: Any, summary: list[str],
) -> None:
    """Show the optional-features menu in a loop until the user continues.

    Features already configured (detected from the existing config)
    start marked ``[✓]`` — re-running onboard shows the real state
    instead of forcing reconfiguration.
    """
    configured: set[str] = _detect_configured_features(config)
    while True:
        choices = [
            _feature_menu_label(key, label, key in configured)
            for key, label, _desc in _OPTIONAL_FEATURES
        ]
        choices.append("→ Continue (finish onboarding)")
        choices.append("× Skip everything (use defaults)")
        pick = q.select(
            "Optional features — enter to configure, → to finish:",
            choices=choices,
        ).ask()
        if pick is None or pick.startswith("×"):
            return
        if pick.startswith("→"):
            return

        # Decode which feature key was clicked.
        feature_key = _decode_feature_key(pick)
        if feature_key is None:
            continue
        did_configure = _configure_feature(feature_key, config, extras, q, summary)
        if did_configure:
            configured.add(feature_key)


def _feature_menu_label(key: str, label: str, configured: bool) -> str:
    mark = "[✓]" if configured else "[ ]"
    return f"{mark} {label}"


def _decode_feature_key(pick: str) -> str | None:
    for key, label, _ in _OPTIONAL_FEATURES:
        if label in pick:
            return key
    return None


def _configure_feature(
    key: str, config: Config, extras: set[str], q: Any, summary: list[str],
) -> bool:
    """Dispatch into the per-feature sub-wizard."""
    if key == "memory":
        return _configure_memory(config, extras, q, summary)
    if key == "vision":
        return _configure_vision(config, q, summary)
    if key == "audio":
        return _configure_audio(config, q, summary)
    if key == "image_gen":
        return _configure_image_gen(config, q, summary)
    if key == "web":
        return _configure_web(config, extras, q, summary)
    if key == "channels":
        return _configure_channels(config, q, summary)
    return False


# ---- Per-feature sub-wizards ---------------------------------------------


def _configure_memory(
    config: Config, extras: set[str], q: Any, summary: list[str],
) -> bool:
    if not q.confirm(
        "Enable vector memory? Adds the `[memory]` extra.",
        default=True,
    ).ask():
        return False
    labels = [c[0] for c in _EMBEDDING_CHOICES]
    pick = q.select("Embedding model:", choices=labels).ask()
    if pick is None:
        return False
    for label, prov, model, _size in _EMBEDDING_CHOICES:
        if label == pick:
            config.memory.embedding = MemoryEmbeddingConfig(provider=prov, model=model)
            break
    extras.add("memory")
    summary.append(f"Vector memory: {pick.split(' (')[0]}")
    return True


def _configure_vision(config: Config, q: Any, summary: list[str]) -> bool:
    if not q.confirm("Configure a dedicated vision model?", default=False).ask():
        return False
    inline_model = q.text(
        "Vision model id (e.g. glm-5v-turbo, gpt-4o-mini, claude-haiku-4-5):"
    ).ask()
    if not inline_model:
        return False
    provider = q.text("Provider for that model (or 'auto'):", default="auto").ask() or "auto"
    config.agents.aux_models = config.agents.aux_models or _empty_aux()
    config.agents.aux_models.vision = AuxModelConfig(model=inline_model, provider=provider)
    summary.append(f"Vision model: {inline_model} ({provider})")
    return True


def _configure_audio(config: Config, q: Any, summary: list[str]) -> bool:
    if not q.confirm("Configure a dedicated audio transcription model?", default=False).ask():
        return False
    inline_model = q.text(
        "Audio model id (e.g. gemini-2.5-flash, whisper-1):"
    ).ask()
    if not inline_model:
        return False
    provider = q.text("Provider for that model (or 'auto'):", default="auto").ask() or "auto"
    config.agents.aux_models = config.agents.aux_models or _empty_aux()
    config.agents.aux_models.audio = AuxModelConfig(model=inline_model, provider=provider)
    summary.append(f"Audio model: {inline_model} ({provider})")
    return True


def _configure_image_gen(config: Config, q: Any, summary: list[str]) -> bool:
    if not q.confirm(
        "Enable image generation tool? (Requires a provider that supports it.)",
        default=False,
    ).ask():
        return False
    summary.append(
        "Image generation: enabled (configure provider with `durin config set ...`)"
    )
    return True


def _configure_web(config: Config, extras: set[str], q: Any, summary: list[str]) -> bool:
    if not q.confirm(
        "Enable web search + fetch tools? Adds the `[web]` extra.",
        default=True,
    ).ask():
        return False
    config.tools.web.enable = True
    extras.add("web")
    summary.append("Web search + fetch: enabled")
    return True


def _configure_channels(config: Config, q: Any, summary: list[str]) -> bool:
    """Configure the gateway's user-facing surfaces.

    Three sub-questions, each opt-in:
    - Daemon mode (run gateway detached so the terminal isn't locked).
    - WebUI dashboard (browser SPA served on the websocket channel).
    - Chat-platform channels (Telegram / Slack / WhatsApp / ...) —
      pointer only; they need per-channel keys we can't ask for here.
    """
    touched_anything = False

    if q.confirm(
        "Run the gateway as a background daemon? "
        "(detached from the terminal; manage with `durin gateway start/stop`)",
        default=False,
    ).ask():
        config.gateway.daemon = True
        summary.append("Gateway daemon: enabled (run with `durin gateway start`)")
        touched_anything = True

    if q.confirm(
        "Enable the WebUI dashboard? (chat in a browser, served by `durin gateway`)",
        default=True,
    ).ask():
        config.gateway.webui_enabled = True
        summary.append("WebUI dashboard: enabled (`durin gateway` will serve it)")
        touched_anything = True
    else:
        config.gateway.webui_enabled = False
        summary.append("WebUI dashboard: disabled")
        touched_anything = True

    if q.confirm(
        "Want to connect chat platforms later (Telegram / Slack / WhatsApp / …)?",
        default=False,
    ).ask():
        summary.append(
            "Chat-platform channels: configure later — see `durin channels status`"
        )
        touched_anything = True

    return touched_anything


def _empty_aux() -> Any:
    from durin.config.schema import AuxModelsConfig

    return AuxModelsConfig()


# ---------------------------------------------------------------------------
# Stage 3 — Workspace
# ---------------------------------------------------------------------------


def _stage_workspace(config: Config, q: Any, summary: list[str]) -> None:
    current = config.agents.defaults.workspace
    new = q.text("Workspace path:", default=current).ask()
    if new and new != current:
        config.agents.defaults.workspace = new
    summary.append(f"Workspace: {config.agents.defaults.workspace}")
