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
    "run_section",
    "SECTIONS",
    "PROVIDER_CHOICES",
    "DEFAULT_MODELS",
    "apply_model_capabilities",
]

# Sections the user can jump straight into via `durin onboard <section>`.
# "model" re-runs provider/key/model + the capability screen; the rest
# map to the optional-feature sub-wizards.
SECTIONS: tuple[str, ...] = (
    "model", "memory", "vision", "audio", "image-gen", "web",
    "dashboard", "channels",
)


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
    availability_lines: list[str] = field(default_factory=list)


def _load_questionary(q: Any | None) -> Any:
    """Return the questionary module (or the injected mock for tests)."""
    if q is not None:
        return q
    try:
        import questionary  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "The onboarding wizard needs the 'questionary' package. "
            "Run `pip install questionary` or use `durin onboard --no-wizard` "
            "to just write defaults."
        ) from exc
    return questionary


def run_wizard(initial_config: Config, *, q: Any | None = None) -> WizardResult:
    """Run the wizard against ``initial_config`` and return the result.

    ``q`` is an injection point for tests: pass a mock with the same
    surface as :mod:`questionary` to drive the wizard programmatically.
    When unset we import questionary lazily (it's an optional dep).
    """
    q = _load_questionary(q)

    config = initial_config.model_copy(deep=True)
    extras: set[str] = set()
    summary: list[str] = []

    # ---- Stage 1: required — provider + default model ---------------
    if not _stage_provider_and_model(config, q, summary):
        return WizardResult(
            config=initial_config, extras_to_install=[],
            cancelled=True, summary_lines=summary,
        )

    # ---- Stage 1b: model capabilities — offer aux models ------------
    _stage_model_capabilities(config, q, summary)

    # ---- Stage 2: optional menu -------------------------------------
    _stage_optional_menu(config, extras, q, summary)

    # ---- Stage 3: workspace (one quick question) --------------------
    _stage_workspace(config, q, summary)

    return WizardResult(
        config=config,
        extras_to_install=sorted(extras),
        cancelled=False,
        summary_lines=summary,
        availability_lines=_build_availability(config),
    )


def run_section(
    initial_config: Config, section: str, *, q: Any | None = None,
) -> WizardResult:
    """Run a single onboarding section — `durin onboard <section>`.

    Lets a user re-tune one thing (the model, channels, memory, …)
    without walking the whole wizard. ``section`` must be one of
    :data:`SECTIONS`.
    """
    q = _load_questionary(q)
    if section not in SECTIONS:
        raise ValueError(
            f"Unknown section '{section}'. Valid: {', '.join(SECTIONS)}"
        )

    config = initial_config.model_copy(deep=True)
    extras: set[str] = set()
    summary: list[str] = []

    if section == "model":
        if not _stage_provider_and_model(config, q, summary):
            return WizardResult(
                config=initial_config, cancelled=True, summary_lines=summary,
            )
        _stage_model_capabilities(config, q, summary)
    else:
        feature_key = section.replace("-", "_")
        _configure_feature(feature_key, config, extras, q, summary)

    return WizardResult(
        config=config,
        extras_to_install=sorted(extras),
        cancelled=False,
        summary_lines=summary,
        availability_lines=_build_availability(config),
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


def _test_model(config: Config) -> None:
    """Run a real round-trip against ``config``'s default model + print the result.

    Pings the *in-memory* config so a model the user just picked (but
    hasn't saved yet) can be verified before the wizard finishes.
    """
    try:
        from durin.cli.doctor import check_model_ping
    except Exception as e:  # noqa: BLE001
        print(f"  Could not run the model test: {e}")
        return
    print("  Testing the model (a real round-trip)…")
    result = check_model_ping(cfg=config)
    if result.status == "ok":
        print(f"  ✓ {result.message}")
    else:
        print(f"  ✗ {result.message}")
        if result.fix:
            print(f"    {result.fix}")


# Sentinel returned by pickers when the user chose "go back" rather
# than a real value or an outright cancel.
_BACK = object()

# Visible, labelled escape hatches — pinned to the TOP of every long
# list so the user never has to scroll a 30-item menu to find the exit.
_BACK_CHOICE = "← Back"
_CANCEL_CHOICE = "✗ Cancel onboarding"


def _pick_provider(
    config: Config, q: Any,
) -> tuple[str, str] | None:
    """Provider picker. Returns ``(name, recommended_model)`` or ``None``.

    Lists every provider durin supports, sorted default → configured →
    rest. A back/cancel row is pinned to the top so the exit is always
    one keypress away, even though the list is ~30 items long. The
    cursor still starts on the ★ default, not on the back row.
    """
    label_to_choice: dict[str, tuple[str, str]] = {}
    rows: list[str] = []
    default_display: str | None = None
    for name, label, configured, is_default in _all_provider_rows(config):
        recommended = next(
            (m for lbl, n, m in PROVIDER_CHOICES if n == name), ""
        )
        tag = "✓ configured" if configured else "— not set"
        if is_default:
            tag = "★ default · " + tag
        display = f"{label:<26} {tag}"
        label_to_choice[display] = (name, recommended)
        rows.append(display)
        if is_default:
            default_display = display

    # When a working provider already exists, backing out returns to
    # the keep/test/change menu; on a fresh install it cancels.
    exit_row = _BACK_CHOICE if _provider_is_configured(config) else _CANCEL_CHOICE
    choices = [exit_row, *rows]
    chosen = q.select(
        "Pick a provider (← top row goes back):",
        choices=choices,
        default=default_display or (rows[0] if rows else exit_row),
    ).ask()
    if chosen is None or chosen == exit_row:
        return None
    return label_to_choice[chosen]


def _pick_model(provider_name: str, recommended_model: str, q: Any) -> Any:
    """Default-model picker. Returns the model id, :data:`_BACK`, or None.

    A back row sits at the top so the user can bounce to the provider
    list instead of being trapped once they've opened the model menu.
    """
    suggestions = list(DEFAULT_MODELS.get(provider_name, ()))
    if recommended_model and recommended_model not in suggestions:
        suggestions.insert(0, recommended_model)
    suggestions = list(dict.fromkeys(suggestions))  # de-dupe, keep order

    other = "Other (type below)"
    if suggestions:
        choices = [_BACK_CHOICE, *suggestions, other]
        pick = q.select(
            "Default model (← top row goes back):",
            choices=choices,
            default=suggestions[0],
        ).ask()
        if pick is None or pick == _BACK_CHOICE:
            return _BACK
        if pick == other:
            typed = q.text("Model name (blank to go back):").ask()
            return typed or _BACK
        return pick

    # No known suggestions (e.g. a custom endpoint) — free-text entry.
    typed = q.text(
        "Model name (blank to pick a different provider):"
    ).ask()
    return typed or _BACK


def _stage_provider_and_model(config: Config, q: Any, summary: list[str]) -> bool:
    """Configure provider + API key + default model. Returns False on cancel.

    The whole stage is one loop so every step has a working "back":
    the model picker bounces to the provider list, the provider list
    bounces to the keep/test/change menu (when a setup already
    exists), and only an explicit cancel ends the wizard. No dead ends.
    """
    while True:
        # ── Keep / test / change menu (only when already configured) ──
        if _provider_is_configured(config):
            d = config.agents.defaults
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
                _test_model(config)
                continue  # back to this menu
            # "Change …" → fall through to the pick flow.

        # ── Provider ──
        picked = _pick_provider(config, q)
        if picked is None:
            # Backed out of the provider list. Loop again: if a setup
            # exists the keep/test/change menu reappears; otherwise
            # there's nothing to keep, so cancel the wizard.
            if _provider_is_configured(config):
                continue
            return False
        provider_name, recommended_model = picked

        # ── API key (local/custom endpoints may not need one) ──
        api_key = q.password(
            f"Paste your {provider_name} API key "
            "(blank if not required, Esc to go back):"
        ).ask()
        if api_key is None:
            continue  # Esc → back to the provider list
        if api_key:
            _set_provider_api_key(config, provider_name, api_key)

        # ── Default model ──
        model_pick = _pick_model(provider_name, recommended_model, q)
        if model_pick is _BACK:
            continue  # back to the provider list
        if model_pick is None:
            return False

        # ── Commit ──
        config.agents.defaults.provider = provider_name
        config.agents.defaults.model = model_pick
        summary.append(f"Provider: {provider_name} ({model_pick})")

        # Sync the context window from the model's capability snapshot.
        for line in apply_model_capabilities(config, model_pick, provider_name):
            summary.append(line)

        # Verify the just-picked model with a real round-trip — catches
        # a typo'd model id or a bad key now, not at the first prompt.
        if q.confirm(
            "Test this model now? (a real round-trip)", default=True,
        ).ask():
            _test_model(config)
        return True


def _set_provider_api_key(config: Config, provider_name: str, api_key: str) -> None:
    """Write the API key into the right ``providers.<name>.api_key`` slot."""
    providers = config.providers
    provider_obj = getattr(providers, provider_name, None)
    if provider_obj is None:
        # Unknown name — treat as `custom` so the key isn't lost.
        provider_obj = providers.custom
    provider_obj.api_key = api_key


def _all_provider_rows(config: Config) -> list[tuple[str, str, bool, bool]]:
    """Every provider durin supports, sorted default → configured → rest.

    Returns ``(name, label, configured, is_default)`` tuples. ``durin``
    supports ~30 providers via the registry — the wizard surfaces all
    of them, not a curated handful, so nothing is hidden.
    """
    from durin.providers.registry import PROVIDERS
    from durin.utils.oauth import any_token_present

    rows: list[tuple[str, str, bool, bool]] = []
    default_name = config.agents.defaults.provider
    for spec in PROVIDERS:
        p = getattr(config.providers, spec.name, None)
        if getattr(spec, "is_oauth", False):
            configured = any_token_present(spec.name)
        elif getattr(spec, "is_local", False):
            configured = bool(p and getattr(p, "api_base", None))
        else:
            configured = bool(p and getattr(p, "api_key", None))
        rows.append((spec.name, spec.label, configured, spec.name == default_name))
    # Sort: the default first, then configured ones, then the rest A-Z.
    rows.sort(key=lambda r: (not r[3], not r[2], r[1].lower()))
    return rows


# ---------------------------------------------------------------------------
# Stage 1b — Model capabilities + aux models
# ---------------------------------------------------------------------------


def _stage_model_capabilities(config: Config, q: Any, summary: list[str]) -> None:
    """Show the default model's modality support and offer aux models.

    durin's main model handles text. Vision (interpret_image) and audio
    (interpret_audio) are handled by *auxiliary* models — used only
    when the main model lacks that modality. This screen surfaces what
    the default model supports and, for any gap, offers to configure
    the corresponding aux model. All optional.

    Note: there is no separate "subagent model" — subagents inherit the
    main model. (If durin grows one, it'd be offered here too.)
    """
    d = config.agents.defaults
    try:
        from durin.providers.capabilities import get_model_capabilities

        caps = get_model_capabilities(d.model, d.provider or None)
    except Exception:  # noqa: BLE001
        return

    has_vision = bool(getattr(caps, "supports_vision", False))
    has_audio = bool(getattr(caps, "supports_audio_input", False))
    aux = getattr(config.agents, "aux_models", None)
    aux_vision = aux is not None and getattr(aux, "vision", None) is not None
    aux_audio = aux is not None and getattr(aux, "audio", None) is not None

    # Show the capability snapshot.
    def _mark(ok: bool) -> str:
        return "✓" if ok else "✗"

    print(f"\n  Default model: {d.model} ({d.provider})")
    print(f"    text {_mark(True)}   vision {_mark(has_vision)}   audio {_mark(has_audio)}")

    # Only offer aux models for gaps. If the main model already does
    # everything, say so and move on — no questions.
    if has_vision and has_audio:
        print("  This model handles text, images and audio on its own — "
              "no auxiliary models needed.")
        return
    print(
        "  An auxiliary model covers a modality the main model lacks "
        "(above). It is\n  used ONLY for that modality — never for normal "
        "chat. Skip if unsure."
    )

    # Vision: offer an aux model only if the main model lacks it and
    # none is configured yet.
    if not has_vision and not aux_vision:
        if q.confirm(
            f"{d.model} can't see images. Configure a vision model "
            "(for the interpret_image tool)?",
            default=False,
        ).ask():
            _ask_aux_model(
                config, q, summary, kind="vision",
                examples="e.g. glm-5v-turbo, gpt-4o-mini, claude-haiku-4-5",
            )
    elif aux_vision:
        summary.append(f"Vision: aux model {config.agents.aux_models.vision.model}")

    # Audio: same logic.
    if not has_audio and not aux_audio:
        if q.confirm(
            f"{d.model} can't hear audio. Configure an audio model "
            "(for the interpret_audio tool)?",
            default=False,
        ).ask():
            _ask_aux_model(
                config, q, summary, kind="audio",
                examples="e.g. gemini-2.5-flash, whisper-1",
            )
    elif aux_audio:
        summary.append(f"Audio: aux model {config.agents.aux_models.audio.model}")


# ---------------------------------------------------------------------------
# Stage 2 — Optional features menu
# ---------------------------------------------------------------------------


# Features the user actively opts into from the Stage-2 menu. Vision
# and audio are deliberately NOT here: they're auxiliary-model
# fallbacks tied to the main model's gaps, so they're offered in
# context by Stage 1b (the capability screen) — not as standalone
# menu items. They still appear in the end-of-wizard matrix and as
# `durin onboard vision|audio` sections.
_OPTIONAL_FEATURES: tuple[tuple[str, str, str], ...] = (
    ("memory", "📁 Vector memory",
     "Semantic recall across sessions. ~130 MB embedding model on first use."),
    ("image_gen", "🎨 Image generation",
     "DALL-E / generate_image tool."),
    ("web", "🔍 Web search + fetch",
     "web_search and web_fetch tools. Pick a search backend."),
    ("dashboard", "🖥️  Web dashboard",
     "Browser chat UI served by `durin gateway`. Independent of channels."),
    ("channels", "💬 Chat channels",
     "Telegram / Slack / Discord / … — bridge the agent to chat platforms."),
)


# Web-search backends durin supports (see durin/agent/tools/web.py).
# (label, provider-id, needs-api-key)
_SEARCH_BACKENDS: tuple[tuple[str, str, bool], ...] = (
    ("DuckDuckGo — no API key, default", "duckduckgo", False),
    ("Brave Search — needs an API key", "brave", True),
    ("Tavily — needs an API key", "tavily", True),
    ("SearXNG — self-hosted, needs a base URL", "searxng", True),
    ("Jina — needs an API key", "jina", True),
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
    if getattr(config.gateway, "webui_enabled", False):
        found.add("dashboard")
    # channels: any channel section with enabled=true.
    extra = getattr(config.channels, "__pydantic_extra__", None) or {}
    for section in extra.values():
        en = section.get("enabled") if isinstance(section, dict) else getattr(section, "enabled", False)
        if en:
            found.add("channels")
            break
    return found


def _build_availability(config: Config) -> list[str]:
    """Build the end-of-wizard capability matrix.

    One line per capability with ``✓`` (works) or ``✗`` (not set up)
    and, for the gaps, the exact `durin onboard <section>` command that
    fills them. The chat model is always ``✓``. Vision/audio show *how*
    they're covered — native to the main model, or via which aux model
    — so the user sees what each configured model actually does.
    """
    configured = _detect_configured_features(config)
    d = config.agents.defaults

    native_vision = native_audio = False
    try:
        from durin.providers.capabilities import get_model_capabilities

        caps = get_model_capabilities(d.model, d.provider or None)
        native_vision = bool(getattr(caps, "supports_vision", False))
        native_audio = bool(getattr(caps, "supports_audio_input", False))
    except Exception:  # noqa: BLE001
        pass

    lines = [f"✓ Chat model — {d.provider} · {d.model}"]

    # Vision / audio: covered natively by the main model, or by an aux
    # model, or not at all.
    aux = getattr(config.agents, "aux_models", None)
    for kind, native, label in (
        ("vision", native_vision, "Vision (image understanding)"),
        ("audio", native_audio, "Audio (transcription)"),
    ):
        aux_model = getattr(getattr(aux, kind, None), "model", None) if aux else None
        if native:
            lines.append(f"✓ {label} — native to {d.model}")
        elif aux_model:
            lines.append(f"✓ {label} — aux model {aux_model}")
        else:
            lines.append(f"✗ {label} — add with `durin onboard {kind}`")

    for key, label, _desc in _OPTIONAL_FEATURES:
        if key in configured:
            lines.append(f"✓ {label}")
        else:
            section = key.replace("_", "-")
            lines.append(f"✗ {label} — add with `durin onboard {section}`")
    return lines


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
    if key == "dashboard":
        return _configure_dashboard(config, q, summary)
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


def _ask_aux_model(
    config: Config, q: Any, summary: list[str], *, kind: str, examples: str,
) -> bool:
    """Prompt for an aux model id + provider and store it. ``kind`` is
    'vision' or 'audio'. No confirm gate — the caller already asked."""
    inline_model = q.text(f"{kind.capitalize()} model id ({examples}):").ask()
    if not inline_model:
        return False
    provider = q.text("Provider for that model (or 'auto'):", default="auto").ask() or "auto"
    config.agents.aux_models = config.agents.aux_models or _empty_aux()
    setattr(
        config.agents.aux_models, kind,
        AuxModelConfig(model=inline_model, provider=provider),
    )
    summary.append(f"{kind.capitalize()} model: {inline_model} ({provider})")
    return True


def _configure_vision(config: Config, q: Any, summary: list[str]) -> bool:
    if not q.confirm("Configure a dedicated vision model?", default=False).ask():
        return False
    return _ask_aux_model(
        config, q, summary, kind="vision",
        examples="e.g. glm-5v-turbo, gpt-4o-mini, claude-haiku-4-5",
    )


def _configure_audio(config: Config, q: Any, summary: list[str]) -> bool:
    if not q.confirm("Configure a dedicated audio transcription model?", default=False).ask():
        return False
    return _ask_aux_model(
        config, q, summary, kind="audio",
        examples="e.g. gemini-2.5-flash, whisper-1",
    )


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
    """Enable web search/fetch and let the user pick + key the backend.

    Web search has several backends (durin/agent/tools/web.py):
    DuckDuckGo needs no key; Brave / Tavily / Jina need an API key;
    SearXNG needs a self-hosted base URL. The wizard surfaces the
    choice instead of silently defaulting.
    """
    if not q.confirm(
        "Enable web search + fetch? `web_search` lets the agent query "
        "the web, `web_fetch` reads a page. Adds the `[web]` extra.",
        default=True,
    ).ask():
        return False
    config.tools.web.enable = True
    extras.add("web")

    labels = [b[0] for b in _SEARCH_BACKENDS]
    pick = q.select("Search backend:", choices=labels).ask()
    if pick is None:
        summary.append("Web search: enabled (DuckDuckGo default)")
        return True
    backend_id, needs_key = next(
        ((bid, nk) for label, bid, nk in _SEARCH_BACKENDS if label == pick),
        ("duckduckgo", False),
    )
    search_cfg = config.tools.web.search
    if hasattr(search_cfg, "provider"):
        search_cfg.provider = backend_id
    if needs_key:
        if backend_id == "searxng":
            base = q.text(f"{backend_id} base URL:").ask()
            if base and hasattr(search_cfg, "base_url"):
                search_cfg.base_url = base
        else:
            key = q.password(f"{backend_id} API key:").ask()
            if key and hasattr(search_cfg, "api_key"):
                search_cfg.api_key = key
    summary.append(f"Web search: {backend_id}")
    return True


def _configure_dashboard(config: Config, q: Any, summary: list[str]) -> bool:
    """Configure the browser dashboard — independent of chat channels.

    The webui is its own surface: `durin gateway` serves it whenever
    `gateway.webui_enabled` is true, regardless of whether any chat
    channel is enabled. Daemon mode (detached gateway) is asked here
    too since it's the same `durin gateway` runtime.
    """
    enable = q.confirm(
        "Enable the web dashboard? (chat with durin in a browser — "
        "served by `durin gateway`, no chat channel needed)",
        default=True,
    ).ask()
    config.gateway.webui_enabled = bool(enable)
    summary.append(
        "Web dashboard: enabled" if enable else "Web dashboard: disabled"
    )

    if q.confirm(
        "Run `durin gateway` as a background daemon? "
        "(detached terminal; manage with `durin gateway start/stop`)",
        default=False,
    ).ask():
        config.gateway.daemon = True
        summary.append("Gateway daemon: enabled")
    return True


# The websocket channel is the web dashboard's transport — `durin
# gateway` enables it automatically when the dashboard is on. It's
# owned by the Dashboard feature, so it's not a "chat channel" the
# user toggles here.
_DASHBOARD_CHANNEL = "websocket"

# Per-channel field that holds the primary credential. Checked in
# order; the first one present in the channel's config is the one we
# prompt for and warn about when left blank.
_CHANNEL_CRED_FIELDS = ("token", "bot_token", "app_id", "appId", "api_key")


def _configure_channels(config: Config, q: Any, summary: list[str]) -> bool:
    """Toggle chat channels on/off — a real two-way switch.

    Channels are discovered from the registry (minus the dashboard's
    websocket transport). Picking an *off* channel turns it on and
    prompts for its primary credential; picking an *on* channel turns
    it off. An enabled channel with a blank credential is flagged so
    the user knows it won't actually connect.
    """
    try:
        from durin.channels.registry import discover_all
    except Exception:  # noqa: BLE001
        summary.append("Channels: registry unavailable — configure via `durin config`")
        return False

    channels = sorted(
        (n, c) for n, c in discover_all().items() if n != _DASHBOARD_CHANNEL
    )
    if not channels:
        summary.append("Channels: none discovered")
        return False

    extra = config.channels.__pydantic_extra__
    if extra is None:
        config.channels.__pydantic_extra__ = extra = {}

    touched = False
    while True:
        rows: list[str] = []
        row_to_name: dict[str, str] = {}
        for name, cls in channels:
            section = extra.get(name)
            en = section.get("enabled") if isinstance(section, dict) else False
            mark = "[green]✓ enabled[/green]" if en else "[dim]— off[/dim]"
            display = cls.display_name if hasattr(cls, "display_name") else name
            row = f"{display:<20} {mark}"
            rows.append(row)
            row_to_name[row] = name
        rows.append("→ Done with channels")
        pick = q.select(
            "Toggle a channel on/off (→ Done to finish):",
            choices=rows,
        ).ask()
        if pick is None or pick.startswith("→"):
            return touched
        name = row_to_name.get(pick)
        if name is None:
            continue
        cls = dict(channels)[name]
        section = extra.get(name)
        if not isinstance(section, dict):
            section = cls.default_config() if hasattr(cls, "default_config") else {}

        if section.get("enabled"):
            # Currently on → turn it off.
            section["enabled"] = False
            extra[name] = section
            summary.append(f"Channel disabled: {name}")
            touched = True
            continue

        # Currently off → turn it on and ask for the primary credential.
        section["enabled"] = True
        cred_field = next((f for f in _CHANNEL_CRED_FIELDS if f in section), None)
        if cred_field is not None:
            val = q.password(f"{name} {cred_field} (blank to skip):").ask()
            if val:
                section[cred_field] = val
            if not section.get(cred_field):
                summary.append(
                    f"⚠ Channel {name}: enabled but {cred_field} is empty — "
                    f"set it with `durin config set channels.{name}.{cred_field} …`"
                )
        extra[name] = section
        summary.append(f"Channel enabled: {name}")
        touched = True


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
