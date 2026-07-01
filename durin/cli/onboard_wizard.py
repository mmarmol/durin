"""Task-oriented onboarding wizard.

The flow is a small **state machine**, not a linear questionnaire:

1. **Direct setup** — when no working provider exists yet, the user is
   walked through provider → API key → model (the minimum durin needs
   to talk to an LLM). Every step has a real "← Back".
2. **Hub** — a re-entrant main menu. Each row is a section (model,
   vision/audio, memory, web, dashboard, channels, workspace) showing
   its current state; opening one drops into a submenu that always
   returns to the hub. No dead ends — the only way out is "Finish".

`durin onboard <section>` jumps straight to one submenu.

The legacy field-by-field walker still lives in ``onboard.py`` and is
reachable via ``durin onboard --advanced``.
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

# Sections reachable directly via `durin onboard <section>`.
SECTIONS: tuple[str, ...] = (
    "model", "vision", "audio", "memory", "web",
    "dashboard", "channels", "workspace",
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
    cap_overrides = {
        k: v.model_dump(exclude_none=True)
        for k, v in (getattr(config, "model_capabilities", {}) or {}).items()
    }
    caps = get_model_capabilities(model, provider or None, overrides=cap_overrides)
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


# (label, internal provider name, recommended default model). Used only
# to pick a sensible default model when the user lands on a provider.
PROVIDER_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("Z.AI Coding Plan (recommended)", "zhipu", "glm-5.1"),
    ("Anthropic (Claude)", "anthropic", "claude-opus-4-7"),
    ("OpenAI (GPT)", "openai", "gpt-5"),
    ("OpenAI Codex (ChatGPT Plus/Pro, OAuth)", "openai_codex", "gpt-5.5"),
    ("Google (Gemini)", "gemini", "gemini-2.5-pro"),
    ("OpenRouter (any model, one key)", "openrouter", "anthropic/claude-opus-4.7"),
    ("Custom OpenAI-compatible endpoint", "custom", ""),
)

# Per-provider model shortlist shown after a provider is picked. Not
# exhaustive and not enforced — every picker has an "Other (type)"
# escape hatch. Wrong/stale ids only cost an inaccurate capability
# hint; they never block the user.
DEFAULT_MODELS: dict[str, tuple[str, ...]] = {
    "zhipu": ("glm-5.1", "glm-4.6", "glm-5-turbo", "glm-4.5v"),
    "zai_coding_plan": ("glm-5.1", "glm-4.6", "glm-5-turbo"),
    "anthropic": ("claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"),
    "openai": ("gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o", "gpt-4o-mini"),
    "openai_codex": ("gpt-5.5", "gpt-5.4-mini", "gpt-5.4", "gpt-5.3-codex", "gpt-5.3-codex-spark"),
    "gemini": ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"),
    "openrouter": (
        "anthropic/claude-opus-4.7",
        "openai/gpt-5",
        "google/gemini-2.5-pro",
    ),
    "deepseek": ("deepseek-chat", "deepseek-reasoner"),
    "moonshot": ("kimi-k2-0905-preview", "moonshot-v1-128k", "moonshot-v1-32k"),
    "minimax": ("MiniMax-M2", "MiniMax-Text-01"),
    "minimax_anthropic": ("MiniMax-M2",),
    "mistral": ("mistral-large-latest", "mistral-small-latest", "pixtral-large-latest"),
    "groq": ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
    "dashscope": ("qwen-max", "qwen-plus", "qwen-vl-max"),
    "xiaomi_mimo": ("mimo-v2",),
    "stepfun": ("step-2-16k", "step-1v-8k"),
    "custom": (),
}

# (label, embedding-provider, embedding-model id, approx download size).
# Every model listed here MUST exist in the fastembed version pinned in
# pyproject.toml OR be registered as a custom model in
# durin/memory/embedding.py (_CUSTOM_MODELS). See
# `tests/memory/test_embedding_catalog.py` for the coherence test that
# pins this list against the runtime catalog.
#
# Simplified to 2 tiers on 2026-05-30. Previous 3-tier wizard offered
# `paraphrase-multilingual-MiniLM-L12-v2` (legacy default) and
# `all-MiniLM-L6-v2` (English-only minimum). Both retired in favor of
# the E5 family — `multilingual-e5-small` is fine-tuned from the same
# base architecture as MiniLM-L12 (MiniLM-L12-H384) but with a
# retrieval-specific contrastive objective; it strictly dominates the
# older MiniLM-L12 on quality at comparable RAM (~200 MB int8 vs
# ~280 MB fp32). MiniLM-L6 (English-only, 90 MB) was niche enough that
# the wizard didn't justify the third row.
_EMBEDDING_CHOICES: tuple[tuple[str, str, str, str], ...] = (
    ("multilingual-e5-small · ~450 MB disk / ~200 MB RAM · default — "
     "100+ languages (incl. Spanish/English/French/CJK), retrieval-"
     "tuned, MIT license. Best balance for personal/laptop use.",
     "fastembed",
     "intfloat/multilingual-e5-small",
     "450 MB"),
    ("multilingual-e5-large · 2.24 GB disk / ~2.5 GB RAM · top quality, "
     "pick if you have a large memory store or need maximum recall on "
     "ambiguous queries — slower install, MIT license.",
     "fastembed",
     "intfloat/multilingual-e5-large",
     "2.24 GB"),
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

# The websocket channel is the dashboard's transport — `durin gateway`
# turns it on automatically when the dashboard is enabled, so it is not
# a "chat channel" the user toggles in the channels submenu.
_DASHBOARD_CHANNEL = "websocket"

# Per-channel field that holds the primary credential, checked in order.
_CHANNEL_CRED_FIELDS = ("token", "bot_token", "app_id", "appId", "api_key")

# Sentinel returned by pickers when the user chose "go back".
_BACK = object()
_BACK_CHOICE = "← Back"
_CANCEL_CHOICE = "✗ Cancel onboarding"


# ---------------------------------------------------------------------------
# Result + entry points
# ---------------------------------------------------------------------------


@dataclass
class WizardResult:
    """Outcome of one wizard run."""

    config: Config
    extras_to_install: list[str] = field(default_factory=list)
    cancelled: bool = False
    summary_lines: list[str] = field(default_factory=list)
    availability_lines: list[str] = field(default_factory=list)


def _durin_questionary_style(questionary: Any) -> Any:
    """A questionary style carrying durin's accent (Ithildin palette).

    The wizard prints onto the user's terminal, so it can't own the
    background — body text stays terminal-default and only the prompts
    (pointer, selection, marker) take durin's accent. See design/DESIGN.md.
    """
    from durin.cli.theme import detect_mode, get_palette

    accent = get_palette("ithildin", detect_mode()).accent
    return questionary.Style(
        [
            ("qmark", f"fg:{accent} bold"),
            ("pointer", f"fg:{accent} bold"),
            ("highlighted", f"fg:{accent} bold"),
            ("selected", f"fg:{accent}"),
            ("answer", f"fg:{accent} bold"),
            ("question", "bold"),
            ("instruction", "fg:ansibrightblack"),
        ]
    )


class _StyledQuestionary:
    """Proxies the questionary module, defaulting durin's style on prompts.

    Themes the whole wizard from one place: every ``select``/``text``/etc.
    call gets ``style=`` injected unless the caller passed its own.
    """

    _PROMPTS = frozenset(
        {"select", "rawselect", "text", "confirm", "checkbox",
         "path", "autocomplete", "password"}
    )

    def __init__(self, module: Any, style: Any) -> None:
        self._module = module
        self._style = style

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._module, name)
        if name in self._PROMPTS and callable(attr):
            style = self._style

            def _styled(*args: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("style", style)
                return attr(*args, **kwargs)

            return _styled
        return attr


def _load_questionary(q: Any | None) -> Any:
    """Return the questionary surface (or the injected mock for tests).

    The real module is wrapped so every prompt picks up durin's accent;
    an injected test mock is returned untouched.
    """
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
    return _StyledQuestionary(questionary, _durin_questionary_style(questionary))


def _reconcile_extras_from_config(config: Config, extras: set[str]) -> None:
    """Make ``extras`` reflect the final config's optional features, however
    the user navigated.

    Vector memory is ON by default, so a user who clicks straight through —
    without ever opening the memory submenu to toggle it — must still get the
    ``[memory]`` extra installed; otherwise the config says enabled but the
    deps are absent and it would silently degrade to grep recall. Same for the
    cross-encoder. The wizard's toggle handlers add these on explicit action;
    this reconcile covers the no-action (accept-the-default) path.
    """
    if getattr(config.memory, "enabled", False):
        extras.add("memory")
    if getattr(config.memory.search.cross_encoder, "enabled", False):
        extras.add("cross-encoder")
    channels = getattr(config, "channels", None)
    if getattr(getattr(channels, "slack", None), "enabled", False):
        extras.add("slack")
    if getattr(getattr(channels, "discord", None), "enabled", False):
        extras.add("discord")
    if config.agents.defaults.provider in ("openai_codex", "github_copilot"):
        extras.add("oauth")
    # Transcription: local provider needs [stt]; [voice] is opt-in via the
    # wizard submenu (record via mic), so it's only added on explicit request.
    transcription = getattr(config, "transcription", None)
    if transcription is not None and getattr(transcription, "enabled", True) and \
            getattr(transcription, "provider", "local") == "local":
        extras.add("stt")
    # TTS: local Supertonic needs [tts]; cloud needs only an API key.
    tts = getattr(config, "tts", None)
    if tts is not None and getattr(tts, "enabled", True) and \
            getattr(tts, "provider", "local") == "local":
        extras.add("tts")


def run_wizard(initial_config: Config, *, q: Any | None = None) -> WizardResult:
    """Run the full wizard: direct setup (if needed) then the hub.

    ``q`` is an injection point for tests: pass a mock with the same
    surface as :mod:`questionary` to drive the wizard programmatically.
    """
    q = _load_questionary(q)
    config = initial_config.model_copy(deep=True)
    extras: set[str] = set()
    summary: list[str] = []

    # A working provider is the one hard requirement — without it durin
    # can't talk to any LLM. Force the direct setup until it's there.
    if not _provider_is_configured(config):
        if not _direct_setup(config, q, summary):
            return WizardResult(
                config=initial_config, cancelled=True, summary_lines=summary,
            )

    _run_hub(config, extras, q, summary)

    _reconcile_extras_from_config(config, extras)
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
    """Open a single hub section directly — `durin onboard <section>`."""
    q = _load_questionary(q)
    if section not in SECTIONS:
        raise ValueError(
            f"Unknown section '{section}'. Valid: {', '.join(SECTIONS)}"
        )

    config = initial_config.model_copy(deep=True)
    extras: set[str] = set()
    summary: list[str] = []

    if section == "model":
        if _provider_is_configured(config):
            _submenu_model(config, q, summary)
        elif not _direct_setup(config, q, summary):
            return WizardResult(
                config=initial_config, cancelled=True, summary_lines=summary,
            )
    elif section in ("vision", "audio"):
        _submenu_vision_audio(config, q, summary)
    elif section == "memory":
        _configure_memory(config, extras, q, summary)
    elif section == "web":
        _configure_web(config, extras, q, summary)
    elif section == "dashboard":
        _configure_dashboard(config, q, summary)
    elif section == "channels":
        _configure_channels(config, q, summary)
    elif section == "workspace":
        _submenu_workspace(config, q, summary)

    _reconcile_extras_from_config(config, extras)
    return WizardResult(
        config=config,
        extras_to_install=sorted(extras),
        cancelled=False,
        summary_lines=summary,
        availability_lines=_build_availability(config),
    )


# ---------------------------------------------------------------------------
# Capability helpers
# ---------------------------------------------------------------------------


def _provider_is_configured(config: Config) -> bool:
    """True when provider + model + (key or local/oauth) are already set."""
    d = config.agents.defaults
    if d.provider == "auto" or not d.model:
        return False
    provider_obj = getattr(config.providers, d.provider, None)
    if provider_obj is None:
        return False
    return bool(
        getattr(provider_obj, "api_key", None)
        or getattr(provider_obj, "api_base", None)
        or d.provider in ("openai_codex", "github_copilot")
    )


def _mark(ok: bool) -> str:
    return "✓" if ok else "✗"


def _model_caps(model: str, provider: str, config: Config | None = None) -> tuple[bool, bool]:
    """Return ``(supports_vision, supports_audio_input)`` for a model."""
    try:
        from durin.providers.capabilities import get_model_capabilities

        cap_overrides = {
            k: v.model_dump(exclude_none=True)
            for k, v in (getattr(config, "model_capabilities", {}) or {}).items()
        } if config is not None else {}
        caps = get_model_capabilities(model, provider or None, overrides=cap_overrides)
        return (
            bool(getattr(caps, "supports_vision", False)),
            bool(getattr(caps, "supports_audio_input", False)),
        )
    except Exception:  # noqa: BLE001
        return (False, False)


def _caps_marks(model: str, provider: str, config: Config | None = None) -> str:
    """A compact ``text✓ vision✗ audio✗`` capability string."""
    vision, audio = _model_caps(model, provider, config=config)
    return f"text✓ vision{_mark(vision)} audio{_mark(audio)}"


def _test_model(config: Config) -> str:
    """Run a real round-trip against ``config``'s default model.

    Returns a one-line result string. Callers inside a menu loop keep
    that line on screen in the next prompt — a bare ``print`` would be
    scrolled away by questionary's redraw.
    """
    try:
        from durin.cli.doctor import check_model_ping
    except Exception as e:  # noqa: BLE001
        line = f"could not run the model test: {e}"
        print(f"  {line}")
        return line
    print("  Testing the model (a real round-trip)…")
    result = check_model_ping(cfg=config)
    if result.status == "ok":
        line = f"✓ {result.message}"
    else:
        line = f"✗ {result.message}"
        if result.fix:
            line += f"  ({result.fix})"
    print(f"  {line}")
    return line


# ---------------------------------------------------------------------------
# Provider / model pickers
# ---------------------------------------------------------------------------


def _all_provider_rows(config: Config) -> list[tuple[str, str, bool, bool]]:
    """Every provider durin supports, sorted default → configured → rest.

    Returns ``(name, label, configured, is_default)`` tuples.
    """
    from durin.providers.registry import PROVIDERS
    from durin.utils.oauth import oauth_token_present

    rows: list[tuple[str, str, bool, bool]] = []
    default_name = config.agents.defaults.provider
    for spec in PROVIDERS:
        p = getattr(config.providers, spec.name, None)
        if getattr(spec, "is_oauth", False):
            # openai_codex keeps its OAuth token in the secret store, not a file
            # path — consult oauth_token_present so codex shows as configured.
            configured = oauth_token_present(spec.name)
        elif getattr(spec, "is_local", False):
            configured = bool(p and getattr(p, "api_base", None))
        else:
            configured = bool(p and getattr(p, "api_key", None))
        rows.append((spec.name, spec.label, configured, spec.name == default_name))
    rows.sort(key=lambda r: (not r[3], not r[2], r[1].lower()))
    return rows


def _store_as_secret(
    *, name: str, value: str, service: str, scope: list[str], description: str = "",
) -> str:
    """Store a wizard credential in the secret store; return its reference.

    Thin wrapper over :func:`durin.security.secrets.store_secret` — the
    plaintext lands only in ``secrets.json`` (0600), never in config.
    """
    from durin.security.secrets import store_secret

    return store_secret(
        name, value, service=service, scope=scope,
        description=description, origin="wizard",
    )


def _set_provider_api_key(config: Config, provider_name: str, api_key: str) -> None:
    """Store the provider API key and put a ``${secret:}`` ref in config."""
    provider_obj = getattr(config.providers, provider_name, None)
    target = provider_name
    if provider_obj is None:
        provider_obj = config.providers.custom
        target = "custom"
    provider_obj.api_key = _store_as_secret(
        name=f"{target}_API_KEY",
        value=api_key,
        service=f"provider:{target}",
        description=f"{target} API key",
        scope=[f"provider:{target}"],
    )


def _pick_provider(config: Config, q: Any) -> tuple[str, str] | None:
    """Provider picker. Returns ``(name, recommended_model)`` or ``None``.

    A back/cancel row is pinned to the top so the exit is always one
    keypress away even though the list is ~30 items long; the cursor
    still starts on the ★ default.
    """
    label_to_choice: dict[str, tuple[str, str]] = {}
    rows: list[str] = []
    default_display: str | None = None
    for name, label, configured, is_default in _all_provider_rows(config):
        recommended = next((m for _l, n, m in PROVIDER_CHOICES if n == name), "")
        tag = "✓ configured" if configured else "— not set"
        if is_default:
            tag = "★ default · " + tag
        display = f"{label:<26} {tag}"
        label_to_choice[display] = (name, recommended)
        rows.append(display)
        if is_default:
            default_display = display

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

    Each suggestion is shown with its capability marks (text/vision/
    audio) so the user can see what a model does before picking it.
    """
    suggestions = list(DEFAULT_MODELS.get(provider_name, ()))
    if recommended_model and recommended_model not in suggestions:
        suggestions.insert(0, recommended_model)
    suggestions = list(dict.fromkeys(suggestions))  # de-dupe, keep order

    other = "Other (type a model id)"
    if suggestions:
        row_to_model: dict[str, str] = {}
        rows: list[str] = []
        for model in suggestions:
            row = f"{model:<30} {_caps_marks(model, provider_name)}"
            row_to_model[row] = model
            rows.append(row)
        choices = [_BACK_CHOICE, *rows, other]
        pick = q.select(
            "Pick the default model (← top row goes back):",
            choices=choices,
            default=rows[0],
        ).ask()
        if pick is None or pick == _BACK_CHOICE:
            return _BACK
        if pick == other:
            typed = q.text("Model id (blank to go back):").ask()
            return typed or _BACK
        return row_to_model.get(pick, pick)

    # No known suggestions (e.g. a custom endpoint) — free-text entry.
    typed = q.text("Model id (blank to pick a different provider):").ask()
    return typed or _BACK


def _commit_model(
    config: Config, provider: str, model: str, summary: list[str],
) -> None:
    """Set provider + model on the config and sync capability-derived knobs."""
    config.agents.defaults.provider = provider
    config.agents.defaults.model = model
    summary.append(f"Provider: {provider} ({model})")
    for line in apply_model_capabilities(config, model, provider):
        summary.append(line)


def _direct_setup(config: Config, q: Any, summary: list[str]) -> bool:
    """Provider → API key → model, as one loop. Returns False on cancel.

    Used both for the first-run forced setup and for "Change provider"
    in the model submenu. Every step has a working back: the model
    picker bounces to the provider list; the provider list cancels.
    """
    while True:
        picked = _pick_provider(config, q)
        if picked is None:
            return False
        provider_name, recommended = picked

        if provider_name == "openai_codex":
            # OAuth provider: authorize via device-code/loopback instead of a key.
            from durin.cli.commands import _codex_login_flow

            try:
                _codex_login_flow(force=None)
            except Exception as exc:  # noqa: BLE001
                summary.append(f"Codex login failed/cancelled: {exc}")
                continue  # back to the provider list
        else:
            api_key = q.password(
                f"Paste your {provider_name} API key "
                "(blank if not required, Esc to go back):"
            ).ask()
            if api_key is None:
                continue  # Esc → back to the provider list
            if api_key:
                _set_provider_api_key(config, provider_name, api_key)

        model = _pick_model(provider_name, recommended, q)
        if model is _BACK:
            continue
        if model is None:
            return False

        _commit_model(config, provider_name, model, summary)
        if q.confirm(
            "Test this model now? (a real round-trip)", default=True,
        ).ask():
            _test_model(config)
        return True


# ---------------------------------------------------------------------------
# The hub
# ---------------------------------------------------------------------------


# (key, label) for each hub row, in display order.
_HUB_ROWS: tuple[tuple[str, str], ...] = (
    ("model", "Model & provider"),
    ("vision-audio", "Vision / audio"),
    ("memory", "Vector memory"),
    ("web", "Web search"),
    ("transcription", "Voice (transcription + speech)"),
    ("dashboard", "Web dashboard"),
    ("channels", "Chat channels"),
    ("workspace", "Workspace"),
)


def _native_modalities(config: Config) -> tuple[bool, bool]:
    """``(vision, audio)`` support of the main default model."""
    d = config.agents.defaults
    return _model_caps(d.model, d.provider, config=config)


def _modality_covered(config: Config, kind: str, native: bool) -> bool:
    """True when *kind* is handled — natively or by an aux model."""
    if native:
        return True
    aux = getattr(config.agents, "aux_models", None)
    return aux is not None and getattr(aux, kind, None) is not None


def _hub_state(key: str, config: Config) -> str:
    """Short status string shown on a hub row."""
    d = config.agents.defaults
    if key == "model":
        return f"{d.provider} · {d.model}"
    if key == "vision-audio":
        nv, na = _native_modalities(config)
        v = _mark(_modality_covered(config, "vision", nv))
        a = _mark(_modality_covered(config, "audio", na))
        return f"vision {v}  audio {a}"
    if key == "memory":
        if getattr(config.memory, "enabled", False):
            return f"on ({config.memory.embedding.model})"
        return "off"
    if key == "web":
        if getattr(config.tools.web, "enable", False):
            backend = getattr(config.tools.web.search, "provider", "") or "duckduckgo"
            return f"on ({backend})"
        return "off"
    if key == "transcription":
        if not getattr(config.transcription, "enabled", True):
            stt = "off"
        else:
            stt = config.transcription.provider
        tts = getattr(getattr(config, "tts", None), "provider", "off")
        return f"stt: {stt} · tts: {tts}"
    if key == "dashboard":
        return "on" if getattr(config.gateway, "webui_enabled", False) else "off"
    if key == "channels":
        extra = getattr(config.channels, "__pydantic_extra__", None) or {}
        on = [
            n for n, s in extra.items()
            if isinstance(s, dict) and s.get("enabled")
        ]
        return ", ".join(sorted(on)) if on else "none"
    if key == "workspace":
        return d.workspace
    return ""


def _run_hub(
    config: Config, extras: set[str], q: Any, summary: list[str],
) -> None:
    """The re-entrant main menu. Loops until the user finishes."""
    last_test = ""
    while True:
        row_to_key: dict[str, str] = {}
        choices: list[str] = []
        for key, label in _HUB_ROWS:
            row = f"{label:<18} ▸ {_hub_state(key, config)}"
            row_to_key[row] = key
            choices.append(row)
        choices.append("Test the model")
        choices.append("✓ Finish onboarding")

        message = "durin onboard — what do you want to set up?"
        if last_test:
            # Keep the last test result visible — questionary would
            # otherwise scroll a bare print off-screen.
            message = f"Last model test: {last_test}\n{message}"

        pick = q.select(message, choices=choices).ask()
        if pick is None or pick.startswith("✓ Finish"):
            return
        if pick == "Test the model":
            last_test = _test_model(config)
            continue
        key = row_to_key.get(pick)
        if key is None:
            continue
        _open_section(key, config, extras, q, summary)


def _open_section(
    key: str, config: Config, extras: set[str], q: Any, summary: list[str],
) -> None:
    """Dispatch a hub row into its submenu."""
    if key == "model":
        _submenu_model(config, q, summary)
    elif key == "vision-audio":
        _submenu_vision_audio(config, q, summary)
    elif key == "memory":
        _configure_memory(config, extras, q, summary)
    elif key == "web":
        _configure_web(config, extras, q, summary)
    elif key == "transcription":
        _configure_transcription(config, extras, q, summary)
    elif key == "dashboard":
        _configure_dashboard(config, q, summary)
    elif key == "channels":
        _configure_channels(config, q, summary)
    elif key == "workspace":
        _submenu_workspace(config, q, summary)


# ---------------------------------------------------------------------------
# Submenu: model & provider
# ---------------------------------------------------------------------------


def _submenu_model(config: Config, q: Any, summary: list[str]) -> None:
    """Change the model (keeping the provider), the provider, or test."""
    last_test = ""
    while True:
        d = config.agents.defaults
        message = (
            f"Model & provider — {d.provider} · {d.model} "
            f"({_caps_marks(d.model, d.provider)}):"
        )
        if last_test:
            message = f"Last model test: {last_test}\n{message}"
        pick = q.select(
            message,
            choices=[
                "Change model only",
                "Change provider",
                "Test the model",
                _BACK_CHOICE,
            ],
        ).ask()
        if pick is None or pick == _BACK_CHOICE:
            return
        if pick == "Test the model":
            last_test = _test_model(config)
            continue
        if pick == "Change model only":
            recommended = next(
                (m for _l, n, m in PROVIDER_CHOICES if n == d.provider), ""
            )
            model = _pick_model(d.provider, recommended, q)
            if model is _BACK or model is None:
                continue
            _commit_model(config, d.provider, model, summary)
            continue
        if pick == "Change provider":
            _direct_setup(config, q, summary)
            continue


# ---------------------------------------------------------------------------
# Submenu: vision / audio aux models
# ---------------------------------------------------------------------------


def _capable_aux_models(config: Config, kind: str) -> list[tuple[str, str]]:
    """``(model, provider)`` pairs that support *kind*, from configured providers.

    The user can only authenticate to a provider they've set up, so the
    vision/audio picker is scoped to configured providers (plus the
    current default provider). Models are matched against durin's
    capability snapshot.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, _label, configured, is_default in _all_provider_rows(config):
        if not configured and not is_default:
            continue
        for model in DEFAULT_MODELS.get(name, ()):
            if model in seen:
                continue
            vision, audio = _model_caps(model, name, config=config)
            if (kind == "vision" and vision) or (kind == "audio" and audio):
                pairs.append((model, name))
                seen.add(model)
    return pairs


def _aux_state(config: Config, kind: str, native: bool) -> str:
    """Status string for a vision/audio row in the submenu."""
    if native:
        return "covered by the main model"
    aux = getattr(config.agents, "aux_models", None)
    entry = getattr(aux, kind, None) if aux else None
    if entry is not None:
        return f"{entry.model} ({entry.provider})"
    return "not set"


def _empty_aux() -> Any:
    from durin.config.schema import AuxModelsConfig

    return AuxModelsConfig()


def _submenu_vision_audio(config: Config, q: Any, summary: list[str]) -> None:
    """Configure auxiliary vision / audio models — used only when the
    main model lacks that modality."""
    while True:
        d = config.agents.defaults
        nv, na = _native_modalities(config)
        print(
            f"\n  Main model {d.model}: "
            f"text ✓  vision {_mark(nv)}  audio {_mark(na)}"
        )
        print(
            "  An auxiliary model covers a modality the main model lacks. "
            "It is used\n  ONLY for that modality (interpret_image / "
            "interpret_audio) — never for chat."
        )
        pick = q.select(
            "Vision / audio:",
            choices=[
                f"Vision  ▸ {_aux_state(config, 'vision', nv)}",
                f"Audio   ▸ {_aux_state(config, 'audio', na)}",
                _BACK_CHOICE,
            ],
        ).ask()
        if pick is None or pick == _BACK_CHOICE:
            return
        kind = "vision" if pick.startswith("Vision") else "audio"
        native = nv if kind == "vision" else na
        _configure_aux_model(config, q, summary, kind=kind, native=native)


def _configure_aux_model(
    config: Config, q: Any, summary: list[str], *, kind: str, native: bool,
) -> None:
    """Pick / change / remove the aux model for *kind* (vision|audio)."""
    aux = getattr(config.agents, "aux_models", None)
    current = getattr(aux, kind, None) if aux else None

    if native and current is None:
        print(f"  The main model already handles {kind} — no aux model needed.")
        return

    pairs = _capable_aux_models(config, kind)
    row_to_pair: dict[str, tuple[str, str]] = {}
    rows: list[str] = []
    for model, prov in pairs:
        row = f"{model:<28} ({prov})"
        row_to_pair[row] = (model, prov)
        rows.append(row)

    other = "Other (type a model id)"
    choices = [_BACK_CHOICE, *rows, other]
    if current is not None:
        choices.append("Remove this aux model")

    pick = q.select(
        f"{kind.capitalize()} model "
        "(from your configured providers, ← Back keeps current):",
        choices=choices,
    ).ask()
    if pick is None or pick == _BACK_CHOICE:
        return
    if pick == "Remove this aux model":
        if aux is not None:
            setattr(aux, kind, None)
        summary.append(f"{kind.capitalize()} aux model removed")
        return
    if pick == other:
        model = q.text(f"{kind.capitalize()} model id (blank to cancel):").ask()
        if not model:
            return
        prov = q.text("Provider for that model (or 'auto'):", default="auto").ask() or "auto"
    else:
        model, prov = row_to_pair[pick]

    config.agents.aux_models = config.agents.aux_models or _empty_aux()
    setattr(
        config.agents.aux_models, kind,
        AuxModelConfig(model=model, provider=prov),
    )
    summary.append(f"{kind.capitalize()} aux model: {model} ({prov})")


# ---------------------------------------------------------------------------
# Submenu: workspace
# ---------------------------------------------------------------------------


def _submenu_workspace(config: Config, q: Any, summary: list[str]) -> None:
    current = config.agents.defaults.workspace
    new = q.text("Workspace path:", default=current).ask()
    if new and new != current:
        config.agents.defaults.workspace = new
        summary.append(f"Workspace: {config.agents.defaults.workspace}")


# ---------------------------------------------------------------------------
# Submenu: memory / web / dashboard / channels
# ---------------------------------------------------------------------------


def _pick_embedding(config: Config, q: Any, summary: list[str]) -> None:
    """Pick the local embedding model. Skippable — ← Back keeps current.

    Only fastembed (local, auto-downloaded) models are offered: that's
    the only embedding backend durin's runtime wires today. Provider-
    hosted embeddings are scaffolded in the schema but not implemented.
    """
    labels = [c[0] for c in _EMBEDDING_CHOICES]
    pick = q.select(
        "Embedding model (← Back keeps the current one):",
        choices=[_BACK_CHOICE, *labels],
    ).ask()
    if pick is None or pick == _BACK_CHOICE:
        return
    for label, prov, model, size in _EMBEDDING_CHOICES:
        if label == pick:
            config.memory.embedding = MemoryEmbeddingConfig(provider=prov, model=model)
            summary.append(f"Embedding model: {label.split(' (')[0]}")
            _maybe_warm_embedding_model(model, size)
            break


def _maybe_warm_embedding_model(model: str, size_label: str) -> None:
    """Pre-download the embedding model now, while the user is attentive.

    Skips silently if fastembed isn't installed yet (the user is still
    in first-onboard with extras not yet present; boot warmup in
    ``AgentLoop`` will handle this case on next start). When fastembed
    is available, runs a synchronous warmup so the user sees the cost
    here — far better than the first ``memory_store`` blocking for ~18
    seconds mid-conversation.
    """
    try:
        from durin.memory.embedding import FastembedProvider, list_supported_models
        list_supported_models()  # raises if fastembed missing
    except (ImportError, RuntimeError):
        print(
            f"  · Model {model} will be downloaded ({size_label}) on first "
            "use, after the [memory] extra is installed."
        )
        return
    print(f"  · Downloading {model} ({size_label}) — first time only...")
    try:
        duration_ms = FastembedProvider.warmup(model=model)
        print(f"    done in {duration_ms / 1000:.1f} s.")
    except Exception as exc:  # noqa: BLE001
        print(f"    warmup failed ({exc}); model will be retried at first use.")


def _configure_memory(
    config: Config, extras: set[str], q: Any, summary: list[str],
) -> None:
    """Vector-memory submenu: toggle it on/off, choose the embedding,
    and configure the search/quality extras (cross-encoder rerank,
    Dream auto-absorb, Dream's auxiliary LLM).

    Re-entrant — every option returns here, and ← Back leaves without
    forcing any choice. Enabling adds the `[memory]` extra; enabling
    cross-encoder adds the `[cross-encoder]` extra (sentence-transformers
    + torch ~1GB). Other options are pure config toggles.

    P10 (2026-05-30): expanded from the prior 2-option menu (toggle +
    embedding) to include the existing `prompt_enable_cross_encoder`,
    `prompt_enable_auto_absorb`, `prompt_memory_aux_model` functions
    that were defined in `onboard_memory.py` but never wired into the
    wizard flow.
    """
    from durin.cli.onboard_memory import (
        prompt_enable_auto_absorb,
        prompt_enable_cross_encoder,
        prompt_memory_aux_model,
    )

    while True:
        on = bool(getattr(config.memory, "enabled", False))
        emb = config.memory.embedding.model
        ce_on = bool(getattr(
            config.memory.search.cross_encoder, "enabled", False,
        ))
        ce_model = getattr(
            config.memory.search.cross_encoder, "model", "",
        )
        absorb_on = bool(getattr(
            config.memory.dream.auto_absorb, "enabled", True,
        ))
        aux_model = getattr(
            config.memory.dream, "model_override", None,
        )
        toggle = "Disable vector memory" if on else "Enable vector memory"
        ce_label = (
            f"Cross-encoder reranker — {'ON' if ce_on else 'off'}"
            f"  ({ce_model})"
        )
        absorb_label = (
            f"Dream auto-absorb — {'ON' if absorb_on else 'off'}"
        )
        aux_label = (
            f"Dream LLM — {'override: ' + aux_model if aux_model else 'same as agent'}"
        )
        pick = q.select(
            f"Vector memory — {'ON' if on else 'off'}  (embedding: {emb}):",
            choices=[
                toggle,
                "Change embedding model",
                ce_label,
                absorb_label,
                aux_label,
                _BACK_CHOICE,
            ],
        ).ask()
        if pick is None or pick == _BACK_CHOICE:
            return
        if pick == toggle:
            config.memory.enabled = not on
            if config.memory.enabled:
                extras.add("memory")
                summary.append("Vector memory: enabled")
            else:
                extras.discard("memory")
                summary.append("Vector memory: disabled")
            continue
        if pick == "Change embedding model":
            _pick_embedding(config, q, summary)
            continue
        if pick == ce_label:
            new_ce = prompt_enable_cross_encoder(current=ce_on)
            config.memory.search.cross_encoder.enabled = new_ce
            if new_ce:
                extras.add("cross-encoder")
                summary.append(
                    f"Cross-encoder reranker: enabled ({ce_model})"
                )
            else:
                extras.discard("cross-encoder")
                summary.append("Cross-encoder reranker: disabled")
            continue
        if pick == absorb_label:
            new_absorb = prompt_enable_auto_absorb(current=absorb_on)
            config.memory.dream.auto_absorb.enabled = new_absorb
            summary.append(
                f"Dream auto-absorb: {'enabled' if new_absorb else 'disabled'}"
            )
            continue
        if pick == aux_label:
            agent_model = getattr(
                config.agents.defaults, "model", "glm-5.1",
            )
            new_aux = prompt_memory_aux_model(
                agent_model=agent_model,
                current=aux_model,
            )
            # `prompt_memory_aux_model` returns:
            #   `agent_model` for "same as agent" → store None (means
            #   "follow agent's model"); user-typed id → store as-is;
            #   None for "skip" → preserve current.
            if new_aux is None:
                pass  # skip
            elif new_aux == agent_model:
                config.memory.dream.model_override = None
                summary.append("Dream LLM: same as agent")
            else:
                config.memory.dream.model_override = new_aux
                summary.append(f"Dream LLM: {new_aux}")
            continue


def _configure_web(
    config: Config, extras: set[str], q: Any, summary: list[str],
) -> bool:
    """Enable web search/fetch and let the user pick + key the backend."""
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
                search_cfg.api_key = _store_as_secret(
                    name=f"{backend_id}_API_KEY",
                    value=key,
                    service="web-search",
                    description=f"{backend_id} web-search API key",
                    scope=["web-search"],
                )
    summary.append(f"Web search: {backend_id}")
    return True


def _configure_transcription(
    config: Config, extras: set[str], q: Any, summary: list[str],
) -> None:
    """Audio transcription submenu: toggle, choose provider, opt into mic
    recording ([voice] extra).

    Re-entrant. Local provider adds ``[stt]``; enabling mic recording adds
    ``[voice]``. Cloud/HTTP providers need no local extra (just an API key
    or base_url set later in config).
    """
    while True:
        t = config.transcription
        on = getattr(t, "enabled", True)
        provider = getattr(t, "provider", "local")
        has_voice = "voice" in extras
        toggle = "Disable transcription" if on else "Enable transcription"
        mic_label = (
            f"TUI mic recording (/voice) — {'ON' if has_voice else 'off'}"
        )
        tts = getattr(config, "tts", None)
        tts_provider = getattr(tts, "provider", "local") if tts is not None else "local"
        tts_label = f"Text-to-speech provider: {tts_provider}"
        pick = q.select(
            f"Audio transcription — {'ON' if on else 'off'}  (provider: {provider}):",
            choices=[
                toggle,
                f"Provider: {provider}",
                mic_label,
                tts_label,
                _BACK_CHOICE,
            ],
        ).ask()
        if pick is None or pick == _BACK_CHOICE:
            return
        if pick == toggle:
            config.transcription.enabled = not on
            continue
        if pick.startswith("Provider:"):
            choice = q.select(
                "Choose a transcription provider:",
                choices=[
                    "Local (offline, fast — [stt] extra)",
                    "Groq (cloud, fast, free tier)",
                    "OpenAI (cloud, whisper-1)",
                    "HTTP server (whisper.cpp / mlx-qwen3-asr / vLLM)",
                    _BACK_CHOICE,
                ],
            ).ask()
            if choice is None or choice == _BACK_CHOICE:
                continue
            if choice.startswith("Local"):
                engine = q.select(
                    "Choose a local STT engine:",
                    choices=[
                        "Parakeet v3 — European langs incl. Spanish/English (default)",
                        "SenseVoice — Chinese / Japanese / Korean / Cantonese",
                        _BACK_CHOICE,
                    ],
                ).ask()
                if engine is None or engine == _BACK_CHOICE:
                    continue
                config.transcription.provider = "local"
                extras.add("stt")
                config.transcription.local.engine = (
                    "sensevoice" if engine.startswith("SenseVoice") else "parakeet"
                )
            elif choice.startswith("Groq"):
                config.transcription.provider = "groq"
                extras.discard("stt")
            elif choice.startswith("OpenAI"):
                config.transcription.provider = "openai"
                extras.discard("stt")
            elif choice.startswith("HTTP"):
                config.transcription.provider = "http"
                extras.discard("stt")
            summary.append(f"Transcription provider: {config.transcription.provider}")
            continue
        if pick == mic_label:
            if has_voice:
                extras.discard("voice")
            else:
                extras.add("voice")
            continue
        if pick == tts_label:
            choice = q.select(
                "Choose a text-to-speech provider:",
                choices=[
                    "Local Supertonic (offline, on-device — [tts] extra)",
                    "OpenAI (cloud)",
                    _BACK_CHOICE,
                ],
            ).ask()
            if choice is None or choice == _BACK_CHOICE:
                continue
            if choice.startswith("Local"):
                config.tts.provider = "local"
                config.tts.enabled = True
                extras.add("tts")
            else:
                config.tts.provider = "openai"
                config.tts.enabled = True
                extras.discard("tts")
            summary.append(f"TTS provider: {config.tts.provider}")
            continue


def _configure_dashboard(config: Config, q: Any, summary: list[str]) -> bool:
    """Configure the browser dashboard — independent of chat channels."""
    enable = q.confirm(
        "Enable the web dashboard? (chat with durin in a browser — "
        "served by `durin gateway`, no chat channel needed)",
        default=True,
    ).ask()
    config.gateway.webui_enabled = bool(enable)
    summary.append("Web dashboard: enabled" if enable else "Web dashboard: disabled")

    if q.confirm(
        "Run `durin gateway` as a background daemon? "
        "(detached terminal; manage with `durin gateway start/stop`)",
        default=False,
    ).ask():
        config.gateway.daemon = True
        summary.append("Gateway daemon: enabled")
    return True


def _configure_channels(config: Config, q: Any, summary: list[str]) -> bool:
    """Toggle chat channels on/off — a real two-way switch.

    Channels are discovered from the registry (minus the dashboard's
    websocket transport). Picking an *off* channel turns it on and
    prompts for its primary credential; picking an *on* channel turns
    it off. An enabled channel left without its credential is flagged.
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
            section["enabled"] = False
            extra[name] = section
            summary.append(f"Channel disabled: {name}")
            touched = True
            continue

        section["enabled"] = True
        cred_field = next((f for f in _CHANNEL_CRED_FIELDS if f in section), None)
        if cred_field is not None:
            val = q.password(f"{name} {cred_field} (blank to skip):").ask()
            if val:
                section[cred_field] = _store_as_secret(
                    name=f"{name}_{cred_field}",
                    value=val,
                    service=f"channel:{name}",
                    description=f"{name} {cred_field}",
                    scope=[f"channel:{name}"],
                )
            if not section.get(cred_field):
                summary.append(
                    f"⚠ Channel {name}: enabled but {cred_field} is empty — "
                    f"set it with `durin config set channels.{name}.{cred_field} …`"
                )
        extra[name] = section
        summary.append(f"Channel enabled: {name}")
        touched = True


# ---------------------------------------------------------------------------
# Detection + end-of-wizard capability matrix
# ---------------------------------------------------------------------------


def _detect_configured_features(config: Config) -> set[str]:
    """Return the feature keys that already look configured."""
    found: set[str] = set()
    aux = getattr(config.agents, "aux_models", None)
    if aux is not None:
        if getattr(aux, "vision", None) is not None:
            found.add("vision")
        if getattr(aux, "audio", None) is not None:
            found.add("audio")
    if getattr(config.memory, "enabled", False):
        found.add("memory")
    if getattr(config.tools.web, "enable", False):
        found.add("web")
    if getattr(config.gateway, "webui_enabled", False):
        found.add("dashboard")
    extra = getattr(config.channels, "__pydantic_extra__", None) or {}
    for section in extra.values():
        en = (
            section.get("enabled") if isinstance(section, dict)
            else getattr(section, "enabled", False)
        )
        if en:
            found.add("channels")
            break
    return found


# (feature key, label) rows for the end-of-wizard matrix.
_MATRIX_FEATURES: tuple[tuple[str, str], ...] = (
    ("memory", "Vector memory"),
    ("web", "Web search + fetch"),
    ("dashboard", "Web dashboard"),
    ("channels", "Chat channels"),
)


def _build_availability(config: Config) -> list[str]:
    """Build the end-of-wizard capability matrix.

    One line per capability with ``✓`` (works) or ``✗`` (not set up).
    Vision/audio show *how* they're covered — native to the main model
    or via which aux model.
    """
    configured = _detect_configured_features(config)
    d = config.agents.defaults
    native_vision, native_audio = _native_modalities(config)

    lines = [f"✓ Chat model — {d.provider} · {d.model}"]

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

    for key, label in _MATRIX_FEATURES:
        if key in configured:
            lines.append(f"✓ {label}")
        else:
            lines.append(f"✗ {label} — add with `durin onboard {key}`")
    return lines
