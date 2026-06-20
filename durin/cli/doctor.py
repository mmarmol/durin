"""`durin doctor` — diagnose install, config, providers, and runtime state.

Runs a battery of small checks and prints a status table. Each check
returns ``ok`` / ``warn`` / ``fail`` with an actionable fix message.
Exit code 0 only when every check is ``ok`` or ``warn`` — ``fail`` flips
the process exit so CI / shell pipelines can gate on it.

Optional behaviour:
- ``--ping``: tests reachability of the active provider's ``api_base``.
- ``--fix``: applies the small subset of fixes that are always safe
  (creating the workspace directory, replaying config migration).
- ``--json``: machine-readable output for scripts.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

import typer
from rich.console import Console
from rich.table import Table

from durin import __version__
from durin.config.loader import get_config_path, load_config

if TYPE_CHECKING:
    from durin.config.schema import Config

console = Console()

Status = Literal["ok", "warn", "fail"]

_STATUS_GLYPH = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}
_STATUS_ORDER = {"ok": 0, "warn": 1, "fail": 2}

_PYTHON_MIN = (3, 11)


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    fix: str | None = None
    category: str = "general"
    # When this result is a missing optional extra, record which extra it
    # belongs to so `--install-missing` can group + install correctly.
    extra: str | None = None
    # When ONE check covers MULTIPLE missing extras (e.g. the
    # "previously installed extras" warn that lists discord, oauth,
    # slack in a single row), put them here so collect_missing_extras
    # picks them all up. Bug pre-2026-05-31: this used to put the
    # extras in the message only, and --install-missing silently
    # noop'd because `extra` was None.
    extras_list: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Individual checks — each returns a CheckResult.
# Checks are intentionally small + isolated so they can be unit-tested.
# ---------------------------------------------------------------------------


def check_python_version() -> CheckResult:
    v = sys.version_info
    if (v.major, v.minor) >= _PYTHON_MIN:
        return CheckResult(
            "python", "ok", f"Python {v.major}.{v.minor}.{v.micro}", category="system",
        )
    return CheckResult(
        "python", "fail",
        f"Python {v.major}.{v.minor}.{v.micro} (need >= {_PYTHON_MIN[0]}.{_PYTHON_MIN[1]})",
        fix=f"Install Python {_PYTHON_MIN[0]}.{_PYTHON_MIN[1]} or newer.",
        category="system",
    )


def check_durin_version() -> CheckResult:
    return CheckResult(
        "durin version", "ok", f"durin {__version__}", category="system",
    )


def check_config_file() -> CheckResult:
    path = get_config_path()
    if not path.exists():
        return CheckResult(
            "config file", "fail",
            f"Missing at {path}",
            fix="Run `durin onboard` (add `--wizard` for the interactive form).",
            category="config",
        )
    try:
        path.read_text(encoding="utf-8")
    except OSError as e:
        return CheckResult("config file", "fail", f"Cannot read {path}: {e}", category="config")
    return CheckResult("config file", "ok", str(path), category="config")


def check_config_parses() -> CheckResult:
    path = get_config_path()
    if not path.exists():
        return CheckResult("config valid", "fail", "No config to validate.", category="config")
    try:
        with path.open(encoding="utf-8") as f:
            json.load(f)
    except json.JSONDecodeError as e:
        return CheckResult(
            "config valid", "fail",
            f"JSON parse error: {e}",
            fix="Edit the file by hand, or back it up and run `durin onboard` to start over.",
            category="config",
        )
    try:
        load_config(path)
    except Exception as e:  # noqa: BLE001 — pydantic ValidationError or downstream
        return CheckResult(
            "config valid", "fail",
            f"Schema validation failed: {e}",
            fix="Run `durin upgrade --migrate-only`, or revert to `~/.durin/config.json.bak`.",
            category="config",
        )
    return CheckResult("config valid", "ok", "Schema validation passed.", category="config")


def check_workspace() -> CheckResult:
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001 — config errors are caught by check_config_parses
        return CheckResult(
            "workspace", "warn", "Could not load config to resolve workspace.", category="config",
        )
    ws = cfg.workspace_path
    if not ws.exists():
        return CheckResult(
            "workspace", "warn",
            f"Missing at {ws}",
            fix="It will be created on first agent run, or run `durin doctor --fix`.",
            category="config",
        )
    if not os.access(ws, os.W_OK):
        return CheckResult(
            "workspace", "fail",
            f"{ws} is not writable.",
            fix=f"chmod +w {ws}",
            category="config",
        )
    return CheckResult("workspace", "ok", str(ws), category="config")


def check_state_dirs_writable() -> CheckResult:
    """Verify the durin home and ~/.cache/durin are writable (or their parents)."""
    from durin.config.home import durin_home

    targets = [durin_home(), Path.home() / ".cache" / "durin"]
    problems: list[str] = []
    for t in targets:
        # Walk up to the first existing ancestor and require it to be writable.
        anchor = t
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent
        if not os.access(anchor, os.W_OK):
            problems.append(f"{anchor} is not writable")
    if problems:
        return CheckResult(
            "state dirs writable", "fail",
            "; ".join(problems),
            fix="Check filesystem permissions on your $HOME.",
            category="config",
        )
    return CheckResult(
        "state dirs writable", "ok",
        "~/.durin and ~/.cache/durin are reachable + writable.",
        category="config",
    )


def check_at_least_one_provider() -> CheckResult:
    """At least one provider must be usable (api_key set, OAuth token, or local backend)."""
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return CheckResult(
            "providers", "warn", "Could not load config to inspect providers.", category="providers",
        )
    from durin.providers.registry import PROVIDERS

    usable: list[str] = []
    for spec in PROVIDERS:
        p = getattr(cfg.providers, spec.name, None)
        if p is None:
            continue
        if spec.is_oauth:
            # OAuth providers report status via `durin provider login` and store
            # tokens outside config.json — we treat them as "usable" if the
            # token file exists. Check this opportunistically.
            if _oauth_token_present(spec.name):
                usable.append(f"{spec.label} (OAuth)")
        elif spec.is_local:
            if p.api_base:
                usable.append(f"{spec.label} ({p.api_base})")
        else:
            if p.api_key:
                usable.append(spec.label)

    if usable:
        return CheckResult(
            "providers", "ok",
            f"{len(usable)} configured: " + ", ".join(usable[:3]) + ("…" if len(usable) > 3 else ""),
            category="providers",
        )
    return CheckResult(
        "providers", "fail",
        "No provider is configured.",
        fix="Set one via `durin config set providers.<vendor>.api_key …` or `durin provider login …`.",
        category="providers",
    )


def _oauth_token_present(provider_name: str) -> bool:
    """Best-effort check for an OAuth token file on disk.

    Delegates to the shared ``durin.utils.oauth`` helper so doctor and
    ``durin status`` agree on whether a provider is actually logged in.
    """
    from durin.utils.oauth import any_token_present

    return any_token_present(provider_name)


def check_default_model_resolvable() -> CheckResult:
    """The default-preset model should resolve to either capabilities or an override."""
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return CheckResult(
            "default model", "warn", "Could not load config.", category="providers",
        )
    try:
        preset = cfg.resolve_preset()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "default model", "fail",
            f"Preset {cfg.agents.defaults.model_preset!r} cannot be resolved: {e}",
            fix="`durin config set agents.defaults.modelPreset default`",
            category="providers",
        )
    model = preset.model
    if not model:
        return CheckResult(
            "default model", "fail",
            "agents.defaults.model is empty.",
            fix="`durin config set agents.defaults.model glm-5.1` (or your preferred model)",
            category="providers",
        )
    return CheckResult("default model", "ok", f"{model} (preset: {cfg.agents.defaults.model_preset!r})", category="providers")


def check_durin_on_path() -> CheckResult:
    """Detect multiple ``durin`` executables on PATH (install shadowing).

    A dev venv install plus a pipx install both on PATH means `durin`
    silently runs whichever sorts first — a confusing split. Surface it.
    """
    seen: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        cand = Path(entry) / "durin"
        if cand.exists() and os.access(cand, os.X_OK) and str(cand) not in seen:
            seen.append(str(cand))
    if len(seen) <= 1:
        return CheckResult(
            "durin on PATH", "ok", seen[0] if seen else "not on PATH",
            category="system",
        )
    return CheckResult(
        "durin on PATH", "warn",
        f"{len(seen)} durin executables on PATH — '{seen[0]}' wins",
        fix=(
            "Multiple installs (e.g. a dev venv + pipx). Remove the stale "
            f"one or fix PATH order. Also found: {', '.join(seen[1:])}"
        ),
        category="system",
    )


def check_secret_refs() -> CheckResult:
    """Verify every ``${secret:}`` reference in config resolves to a secret.

    A dangling reference (config points at a name the store lacks) would
    otherwise surface as a confusing provider/channel failure mid-task.
    """
    from durin.config.loader import read_persisted_config
    from durin.security.secrets import SecretStore, is_secret_ref, parse_secret_ref

    try:
        data = read_persisted_config()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "secret refs", "warn", f"Could not read config: {e}", category="config"
        )

    refs: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)
        elif is_secret_ref(node):
            name = parse_secret_ref(node)
            if name:
                refs.append(name)

    _walk(data)
    if not refs:
        return CheckResult(
            "secret refs", "ok", "no ${secret:} references", category="config"
        )

    store = SecretStore().load()
    dangling = sorted({name for name in refs if store.get(name) is None})
    if dangling:
        return CheckResult(
            "secret refs", "fail",
            f"{len(dangling)} dangling reference(s): {', '.join(dangling)}",
            fix="Add the missing secret(s): `durin secret set <NAME> --service <S>`.",
            category="config",
        )
    return CheckResult(
        "secret refs", "ok",
        f"{len(refs)} reference(s), all resolve",
        category="config",
    )


def check_executable(name: str, *, required: bool, hint: str) -> CheckResult:
    found = shutil.which(name)
    if found:
        return CheckResult(name, "ok", found, category="tools")
    status: Status = "fail" if required else "warn"
    return CheckResult(name, status, f"`{name}` not on PATH", fix=hint, category="tools")


def check_optional_extra(import_name: str, *, extra: str, purpose: str) -> CheckResult:
    """Verify an optional extra's import works. Always returns ok/warn (never fail)."""
    try:
        importlib.import_module(import_name)
        return CheckResult(import_name, "ok", f"{import_name} importable", category="extras", extra=extra)
    except ImportError:
        from durin.cli.upgrade import install_hint

        return CheckResult(
            import_name, "warn",
            f"Not installed — needed for: {purpose}",
            fix=install_hint([extra]),
            category="extras",
            extra=extra,
        )


def check_cross_encoder_dep() -> CheckResult:
    """P11 Fix A (2026-05-30): conditional check for `sentence_transformers`
    when the user has cross-encoder rerank enabled in config.

    Pattern: the H25 audit found that durin shipped with cross-encoder
    rerank in the code path but `sentence-transformers` was never in
    any default extra — operators who flipped the toggle silently saw
    no improvement (CE failed to load, RRF fallback engaged). This
    check makes the gap loud the next time someone runs `durin doctor`.

    States:
    - CE disabled in config → status "ok" (silent — most users don't
      need CE, no point in warn clutter).
    - CE enabled + sentence_transformers OK → status "ok".
    - CE enabled + sentence_transformers missing → status "warn" with
      `pip install durin-agent[cross-encoder]` hint; doctor's
      `--install-missing` can auto-fix.
    """
    try:
        cfg = load_config()
        ce_enabled = bool(cfg.memory.search.cross_encoder.enabled)
    except Exception:  # noqa: BLE001
        # If config can't load, other checks will surface that;
        # don't double-report here.
        return CheckResult(
            "cross-encoder dep", "ok",
            "config not loaded yet — skipped",
            category="extras", extra="cross-encoder",
        )
    if not ce_enabled:
        return CheckResult(
            "cross-encoder dep", "ok",
            "cross-encoder disabled in config — skipped",
            category="extras", extra="cross-encoder",
        )
    try:
        importlib.import_module("sentence_transformers")
        return CheckResult(
            "cross-encoder dep", "ok",
            "sentence_transformers importable (cross-encoder rerank active)",
            category="extras", extra="cross-encoder",
        )
    except ImportError:
        from durin.cli.upgrade import install_hint

        return CheckResult(
            "cross-encoder dep", "warn",
            "sentence_transformers NOT installed but "
            "memory.search.cross_encoder.enabled=true. Search will "
            "fall through to pure RRF (no rerank). Install the "
            "cross-encoder extra to enable the rerank step.",
            fix=install_hint(["cross-encoder"]),
            category="extras", extra="cross-encoder",
        )


def check_stt_installed() -> CheckResult:
    """Verify the [stt] extra (sherpa-onnx) is importable for local
    transcription (spec §8.1). Always returns ok/warn, never fails."""
    return check_optional_extra(
        "sherpa_onnx",
        extra="stt",
        purpose="fast local ASR (Parakeet/SenseVoice)",
    )


def check_stt_model_cached(cfg: "Config | None" = None) -> CheckResult:
    """Warn (never fail) if the configured local engine's model isn't cached."""
    try:
        from durin.config.loader import load_config
        from durin.providers.stt_models import ENGINES, _default_stt_cache
        config = cfg or load_config()
    except Exception:  # noqa: BLE001
        return CheckResult("stt.model_cached", "ok", "skipped", category="stt")
    if config.transcription.provider != "local":
        return CheckResult("stt.model_cached", "ok", "cloud provider", category="stt")
    engine = config.transcription.local.engine
    spec = ENGINES.get(engine)
    if spec is None:
        return CheckResult(
            "stt.model_cached", "warn",
            f"unknown engine {engine!r} in config (known: {', '.join(ENGINES)})",
            fix="Set transcription.local.engine to one of: " + ", ".join(ENGINES),
            category="stt",
        )
    eng_dir = _default_stt_cache() / spec.dir_name
    if (eng_dir / spec.files["tokens"]).exists():
        return CheckResult("stt.model_cached", "ok", f"{engine} model cached", category="stt")
    return CheckResult(
        "stt.model_cached", "warn",
        f"{engine} model not cached — first transcription downloads it",
        fix="Run a transcription once, or `durin doctor --ping-model`.",
        category="stt",
    )


def check_voice_extra() -> CheckResult:
    """Verify the [voice] extra (sounddevice) is importable for TUI mic
    recording (spec §8.1). Always returns ok/warn, never fails."""
    return check_optional_extra(
        "sounddevice",
        extra="voice",
        purpose="TUI microphone recording (/voice)",
    )


def check_stt_cloud_keys(cfg: "Config | None" = None) -> CheckResult:
    """When transcription.provider is a cloud backend, verify an API key is set.

    The local provider (default) needs no key, so this is ok-by-default and
    only warns when the user picked groq/openai/http without a key.
    """
    try:
        from durin.config.loader import load_config

        config = cfg or load_config()
    except Exception:  # noqa: BLE001
        return CheckResult(
            "stt.cloud_keys", "ok",
            "transcription config unreadable; skipping cloud-key check",
            category="stt",
        )

    provider = config.transcription.provider
    if provider == "local":
        return CheckResult(
            "stt.cloud_keys", "ok",
            "local transcription (no API key needed)",
            category="stt",
        )

    # Cloud providers: resolve the relevant key.
    if provider == "groq":
        key = config.transcription.groq.api_key
    elif provider == "openai":
        key = config.transcription.openai.api_key
    else:  # http
        key = config.transcription.http.api_key

    if key:
        return CheckResult(
            "stt.cloud_keys", "ok",
            f"{provider} transcription API key configured",
            category="stt",
        )
    return CheckResult(
        "stt.cloud_keys", "warn",
        f"transcription.provider={provider!r} but no API key set — "
        "transcription will return empty text",
        fix="Set the key under transcription.<provider>.api_key in config, "
        "or switch transcription.provider to 'local' (needs the [stt] extra).",
        category="stt",
    )


def check_cache_size() -> CheckResult:
    cache = Path.home() / ".cache" / "durin"
    if not cache.exists():
        return CheckResult("cache size", "ok", "no cache yet", category="state")
    total = 0
    for root, _dirs, files in os.walk(cache, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    gb = total / (1024 ** 3)
    if gb > 10:
        return CheckResult(
            "cache size", "warn",
            f"{gb:.1f} GB at {cache}",
            fix="`durin uninstall --keep-config --keep-workspace --yes` to drop caches.",
            category="state",
        )
    if gb > 1:
        return CheckResult("cache size", "ok", f"{gb:.2f} GB at {cache}", category="state")
    mb = total / (1024 ** 2)
    return CheckResult("cache size", "ok", f"{mb:.1f} MB at {cache}", category="state")


# Map of extra-name → list of import names that prove the extra is present.
# Used by `detect_installed_extras` for drift tracking.
_EXTRAS_IMPORT_PROBES: dict[str, tuple[str, ...]] = {
    "memory": ("fastembed", "lancedb"),
    "mcp": ("mcp",),
    "web": ("ddgs", "readability"),
    "oauth": ("oauth_cli_kit",),
    "slack": ("slack_sdk",),
    "discord": ("discord",),
    "local": ("llama_cpp", "huggingface_hub"),
}


def _module_importable(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def detect_installed_extras() -> list[str]:
    """Return the names of extras whose modules are all importable now.

    "Currently installed" is approximated by import success — that's the
    same probe ``check_optional_extra`` uses, so the two checks stay in
    sync.
    """
    found: list[str] = []
    for extra, modules in _EXTRAS_IMPORT_PROBES.items():
        if all(_module_importable(m) for m in modules):
            found.append(extra)
    return found


def update_extras_state(*, save: Callable[..., Any] | None = None) -> set[str] | None:
    """Append any newly-detected extras to ``config.install.extras``.

    *Additive* — never removes entries. That way `pipx uninstall` →
    `pipx install` doesn't silently forget the user used to have memory
    installed; the next `durin doctor` will surface the gap.

    Returns the new union, or ``None`` if no update was needed.
    """
    from durin.config.loader import save_config

    cfg = load_config()
    current = set(detect_installed_extras())
    saved = set(cfg.install.extras or [])
    union = saved | current
    if union == saved:
        return None
    cfg.install.extras = sorted(union)
    (save or save_config)(cfg)
    return union


def check_extras_drift() -> CheckResult:
    """Detect extras the user previously had but no longer does.

    durin keeps a running list of optional extras (memory / mcp / etc.)
    you've had installed at some point — in ``config.install.extras``.
    This check compares that list against what's currently importable
    and warns when something dropped (typically a fresh `pipx install`
    after an uninstall, which wipes extras).
    """
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return CheckResult(
            "previously installed extras", "warn",
            "Could not load config.",
            category="extras",
        )
    tracked = set(cfg.install.extras or [])
    if not tracked:
        return CheckResult(
            "previously installed extras", "ok",
            "none tracked yet — durin will start remembering now",
            category="extras",
        )
    current = set(detect_installed_extras())
    missing = tracked - current
    if missing:
        sorted_missing = tuple(sorted(missing))
        names = ", ".join(sorted_missing)
        return CheckResult(
            "previously installed extras", "warn",
            f"{names} (you had it before but it's gone now)",
            fix=f"`durin doctor --install-missing -y` to restore {names}.",
            category="extras",
            extras_list=sorted_missing,
        )
    return CheckResult(
        "previously installed extras", "ok",
        f"{len(tracked)} present — {', '.join(sorted(tracked))}",
        category="extras",
    )


def check_embedding_model() -> CheckResult:
    """Validate ``config.memory.embedding.model`` against fastembed's catalog.

    Three outcomes, with actionable messages:

    - ``memory.enabled = False`` → ``ok``, "not enabled, vector retrieval off".
    - memory enabled but fastembed not importable → ``warn``, points to
      ``check_optional_extra`` which already flags the missing extra.
    - fastembed present and model in catalog → ``ok`` with model + dim.
    - fastembed present but model NOT in catalog → ``fail`` with the
      list of supported models (the same actionable message
      ``FastembedProvider.__init__`` would emit).
    """
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return CheckResult(
            "embedding model", "warn",
            "Could not load config.",
            category="state",
        )
    if not getattr(cfg.memory, "enabled", False):
        return CheckResult(
            "embedding model", "ok",
            "vector memory off (set memory.enabled=true to use it)",
            category="state",
        )
    model_name = cfg.memory.embedding.model
    try:
        from durin.memory.embedding import list_supported_models
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "embedding model", "warn",
            f"could not load fastembed catalog: {exc}",
            fix="`durin doctor --install-missing -y` to install [memory] extra.",
            category="state",
        )
    try:
        catalog = list_supported_models()
    except RuntimeError:
        # fastembed not installed — the dedicated extras checks already
        # cover this; we don't double-fail.
        return CheckResult(
            "embedding model", "ok",
            f"configured as {model_name} ([memory] extra not installed)",
            category="state",
        )
    if model_name in catalog:
        dim = catalog[model_name].get("dim", "?")
        size = catalog[model_name].get("size_in_GB")
        size_str = f", ~{size:.2f} GB" if isinstance(size, (int, float)) else ""
        return CheckResult(
            "embedding model", "ok",
            f"{model_name} ({dim}-dim{size_str})",
            category="state",
        )
    available_sample = ", ".join(sorted(catalog)[:5])
    return CheckResult(
        "embedding model", "fail",
        f"{model_name!r} is not in fastembed's catalog (got {len(catalog)} models, "
        f"e.g. {available_sample}…)",
        fix=(
            "Set memory.embedding.model to one of the wizard options, or "
            "rerun `durin onboard` and pick a model from the menu."
        ),
        category="state",
    )


def check_embedding_model_loads() -> CheckResult:
    """P11 Fix E (2026-05-30): smoke-test that the configured embedding
    model actually loads + produces a vector.

    `check_embedding_model` validates the model id against fastembed's
    catalog at the boundary; this check goes further — it does a real
    load + embed of a trivial input. Catches:

    - Corrupt fastembed cache (`~/.cache/fastembed/` partially
      downloaded after a failed first run)
    - Network unreachable at install time (model exists in catalog
      but isn't on disk yet, no internet to fetch)
    - ONNX runtime mismatch (rare; fastembed pinned narrowly to
      avoid this — see `pyproject.toml` comment)

    Skip cases (status="skipped") to avoid noise:
    - memory.enabled = false (most users)
    - fastembed not installed (already covered by check_optional_extra)
    """
    try:
        cfg = load_config()
        if not cfg.memory.enabled:
            return CheckResult(
                "embedding model load", "ok",
                "memory disabled in config — skipped",
                category="state",
            )
        model_id = cfg.memory.embedding.model
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "embedding model load", "ok",
            f"config load failed: {exc}",
            category="state",
        )
    try:
        from durin.memory.embedding import FastembedProvider
    except Exception:  # noqa: BLE001
        return CheckResult(
            "embedding model load", "ok",
            "fastembed not importable — covered by extras check",
            category="state",
        )
    # The provider class imports even without the `[memory]` extra
    # (fastembed loads lazily inside it), so the import above does not
    # catch a missing dependency. Probe fastembed directly: a not-installed
    # extra is a skip, not a doctor failure — CI deliberately runs without
    # `[memory]` (fastembed downloads ~2GB of models).
    try:
        import fastembed  # noqa: F401, PLC0415
    except ImportError:
        return CheckResult(
            "embedding model load", "ok",
            "fastembed not installed — covered by extras check",
            category="state",
        )
    try:
        provider = FastembedProvider(model=model_id)
        vec = provider.embed(["health probe"])[0]
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "embedding model load", "fail",
            f"load+embed raised: {type(exc).__name__}: {exc}",
            fix=(
                "Check `~/.cache/fastembed/` for partial downloads "
                "(rm -rf and re-run); confirm internet reachable; or "
                "switch model via `durin onboard` memory submenu."
            ),
            category="state",
        )
    if not isinstance(vec, list) or len(vec) == 0:
        return CheckResult(
            "embedding model load", "fail",
            f"embed returned unusable result: {type(vec).__name__}({vec!r})",
            category="state",
        )
    return CheckResult(
        "embedding model load", "ok",
        f"{model_id} produced {len(vec)}-dim vector",
        category="state",
    )


def check_cross_encoder_loads() -> CheckResult:
    """P11 Fix E (2026-05-30): smoke-test that the configured
    cross-encoder model actually loads + scores a pair.

    Skip cases:
    - `memory.search.cross_encoder.enabled = false` (most users)
    - sentence_transformers not installed (covered by
      `check_cross_encoder_dep`)
    """
    try:
        cfg = load_config()
        ce_cfg = cfg.memory.search.cross_encoder
        if not ce_cfg.enabled:
            return CheckResult(
                "cross-encoder load", "ok",
                "cross-encoder disabled in config — skipped",
                category="state",
            )
        model_id = ce_cfg.model
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "cross-encoder load", "ok",
            f"config load failed: {exc}",
            category="state",
        )
    try:
        importlib.import_module("sentence_transformers")
    except ImportError:
        return CheckResult(
            "cross-encoder load", "ok",
            "sentence_transformers missing — covered by extras check",
            category="state",
        )
    try:
        from durin.memory.cross_encoder import CrossEncoderReranker
        probe = CrossEncoderReranker(model=model_id)
        scores = probe.score("health probe", ["dummy doc"])
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "cross-encoder load", "fail",
            f"load+score raised: {type(exc).__name__}: {exc}",
            fix=(
                "Check `~/.cache/huggingface/` for partial downloads; "
                "confirm internet reachable; or switch model via the "
                "webui memory settings (model picker)."
            ),
            category="state",
        )
    if not scores:
        return CheckResult(
            "cross-encoder load", "fail",
            "load returned no scores (see ERROR log for root cause)",
            fix=(
                "Likely sentence_transformers missing or model "
                "unreachable. `pip install durin-agent[cross-encoder]`."
            ),
            category="state",
        )
    return CheckResult(
        "cross-encoder load", "ok",
        f"{model_id} produced score {float(scores[0]):.3f}",
        category="state",
    )


def check_memory_summary() -> CheckResult:
    """Quantify the installation: how much memory / history is on disk."""
    try:
        cfg = load_config()
        workspace = cfg.workspace_path
    except Exception:  # noqa: BLE001
        return CheckResult("memory store", "warn", "Could not load config.", category="state")

    from durin.cli.tui.startup import memory_summary

    stats = memory_summary(workspace)
    pieces = []
    pieces.append(f"{stats['memory_docs']} memory docs")
    if stats["ingested_docs"]:
        pieces.append(f"{stats['ingested_docs']} ingested")
    if stats["vec_present"]:
        pieces.append("vector index present")
    pieces.append(f"{stats['sessions']} sessions")
    pieces.append(f"{stats['skills']} skills")
    return CheckResult(
        "memory store", "ok",
        " · ".join(pieces),
        category="state",
    )


async def check_model_ping_async(
    *, timeout: float = 15.0, cfg: "Config | None" = None
) -> CheckResult:
    """Async core of :func:`check_model_ping`.

    A 3-token round-trip via the same provider/client the agent uses.
    Safe to ``await`` from inside a running event loop (the gateway's
    HTTP handlers need this). Pass ``cfg`` to ping an in-memory config.
    """
    import asyncio

    if cfg is None:
        try:
            cfg = load_config()
        except Exception as e:  # noqa: BLE001
            return CheckResult(
                "model ping", "fail", f"Could not load config: {e}", category="providers"
            )

    try:
        from durin.providers.factory import make_provider

        provider = make_provider(cfg)
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "model ping", "fail",
            f"Could not build provider: {e}",
            fix="`durin config get providers` to inspect provider config.",
            category="providers",
        )

    async def _ping() -> str | None:
        try:
            resp = await provider.chat(
                messages=[{"role": "user", "content": "ping"}],
                model=cfg.resolve_preset().model,
                max_tokens=4,
                temperature=0.0,
            )
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"
        if getattr(resp, "content", None) is None and not getattr(resp, "tool_calls", None):
            return "empty response"
        return None  # success

    try:
        err = await asyncio.wait_for(_ping(), timeout=timeout)
    except asyncio.TimeoutError:
        return CheckResult(
            "model ping", "fail",
            f"Timed out after {timeout:.0f}s",
            fix="Check network or raise `DURIN_OPENAI_COMPAT_TIMEOUT_S`.",
            category="providers",
        )
    except Exception as e:  # noqa: BLE001
        return CheckResult("model ping", "fail", f"{type(e).__name__}: {e}", category="providers")

    if err:
        return CheckResult(
            "model ping", "fail",
            err,
            fix="`durin config set providers.<vendor>.apiKey ...` if auth, otherwise inspect logs.",
            category="providers",
        )
    return CheckResult(
        "model ping", "ok",
        f"{cfg.resolve_preset().model} responded.",
        category="providers",
    )


def check_model_ping(*, timeout: float = 15.0, cfg: "Config | None" = None) -> CheckResult:
    """`--ping-model`: a real round-trip to the configured default model.

    Sync wrapper over :func:`check_model_ping_async`. Must NOT be called
    from inside a running event loop — use the async form there.
    """
    import asyncio

    return asyncio.run(check_model_ping_async(timeout=timeout, cfg=cfg))


def check_gateway_daemon() -> CheckResult:
    """When ``config.gateway.daemon=true``, the PID file should point at a live process.

    Skipped (ok-message) when daemon mode isn't requested — running the
    gateway in foreground is a totally valid choice, not a problem.
    """
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return CheckResult(
            "gateway daemon", "warn",
            "Could not load config to check daemon state.",
            category="services",
        )
    if not getattr(cfg.gateway, "daemon", False):
        return CheckResult(
            "gateway daemon", "ok",
            "daemon mode disabled (gateway.daemon=false)",
            category="services",
        )
    from durin.cli.gateway_daemon import daemon_status

    s = daemon_status()
    if s.state == "running":
        return CheckResult(
            "gateway daemon", "ok",
            f"running (pid {s.pid})",
            category="services",
        )
    if s.state == "stale_pid":
        return CheckResult(
            "gateway daemon", "fail",
            "config requests daemon mode but the PID file points at a dead process.",
            fix="`durin gateway start` to relaunch.",
            category="services",
        )
    return CheckResult(
        "gateway daemon", "fail",
        "config requests daemon mode but the gateway is not running.",
        fix="`durin gateway start` to launch it.",
        category="services",
    )


def check_webui_reachable(*, timeout: float = 1.5) -> CheckResult:
    """When ``config.gateway.webui_enabled=true``, the dashboard must respond on the websocket channel's port.

    Skipped when the webui isn't requested.
    """
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return CheckResult(
            "webui dashboard", "warn",
            "Could not load config to check webui state.",
            category="services",
        )
    if not getattr(cfg.gateway, "webui_enabled", False):
        return CheckResult(
            "webui dashboard", "ok",
            "webui disabled (gateway.webui_enabled=false)",
            category="services",
        )
    # Figure out where the webui is served. Defaults match the
    # websocket channel's defaults (host 127.0.0.1, port 8765); live
    # overrides come from config.channels.websocket.
    ws_section = getattr(cfg.channels, "websocket", None)
    host = "127.0.0.1"
    ws_port = 8765
    if ws_section is not None:
        if isinstance(ws_section, dict):
            host = ws_section.get("host", host)
            ws_port = ws_section.get("port", ws_port)
        else:
            host = getattr(ws_section, "host", host) or host
            ws_port = getattr(ws_section, "port", ws_port) or ws_port
    target = f"http://{host}:{ws_port}/"
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            r = client.get(target)
            # The dashboard root must serve the SPA's index.html — a 2xx.
            # A 404 means the webui bundle is missing from the install
            # (durin/web/dist/ wasn't shipped); that is NOT healthy even
            # though the port is listening.
            if 200 <= r.status_code < 300:
                return CheckResult(
                    "webui dashboard", "ok",
                    f"serving at {target}",
                    category="services",
                )
            if r.status_code == 404:
                return CheckResult(
                    "webui dashboard", "fail",
                    f"{target} → 404 — the webui bundle isn't installed.",
                    fix="Reinstall a build that bundles durin/web/dist/ "
                        "(the wheel must include the SPA).",
                    category="services",
                )
            return CheckResult(
                "webui dashboard", "fail",
                f"{target} → HTTP {r.status_code}",
                fix="Check the gateway log: `durin gateway logs`",
                category="services",
            )
    except Exception:  # noqa: BLE001 — any network error
        return CheckResult(
            "webui dashboard", "fail",
            f"not reachable at {target}",
            fix="`durin gateway start` (or `durin gateway` if not in daemon mode).",
            category="services",
        )


def check_provider_reachable(*, timeout: float = 3.0) -> CheckResult:
    """`--ping`: HEAD/GET against the configured provider's api_base."""
    try:
        cfg = load_config()
        preset = cfg.resolve_preset()
    except Exception:  # noqa: BLE001
        return CheckResult("provider ping", "warn", "Could not resolve active provider.", category="providers")
    from durin.providers.registry import find_by_name

    spec_name = preset.provider if preset.provider != "auto" else None
    if not spec_name:
        return CheckResult(
            "provider ping", "warn",
            "agents.defaults.provider is 'auto'; skipping ping.",
            category="providers",
        )
    spec = find_by_name(spec_name)
    if spec is None:
        return CheckResult("provider ping", "warn", f"unknown provider {spec_name!r}", category="providers")
    p = getattr(cfg.providers, spec.name, None)
    base = p.api_base if p and getattr(p, "api_base", None) else getattr(spec, "default_api_base", None)
    if not base:
        return CheckResult("provider ping", "warn", f"{spec.label}: no api_base set.", category="providers")

    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            r = client.get(base)
            return CheckResult(
                "provider ping", "ok",
                f"{spec.label} HTTP {r.status_code} ({base})",
                category="providers",
            )
    except Exception as e:  # noqa: BLE001 — network errors of any flavor
        return CheckResult(
            "provider ping", "fail",
            f"{spec.label} unreachable at {base}: {e}",
            fix="Check the api_base + network. Some endpoints reject bare GET — see HTTP code.",
            category="providers",
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class DoctorReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, r: CheckResult) -> None:
        self.results.append(r)

    @property
    def worst(self) -> Status:
        if not self.results:
            return "ok"
        return max(self.results, key=lambda r: _STATUS_ORDER[r.status]).status

    @property
    def counts(self) -> dict[Status, int]:
        c: dict[Status, int] = {"ok": 0, "warn": 0, "fail": 0}
        for r in self.results:
            c[r.status] += 1
        return c


def run_checks(*, ping: bool = False, ping_model: bool = False) -> DoctorReport:
    report = DoctorReport()
    report.add(check_python_version())
    report.add(check_durin_version())
    report.add(check_durin_on_path())
    report.add(check_config_file())
    report.add(check_config_parses())
    report.add(check_workspace())
    report.add(check_state_dirs_writable())
    report.add(check_at_least_one_provider())
    report.add(check_default_model_resolvable())
    report.add(check_secret_refs())
    report.add(check_executable("git", required=False, hint="Install git so `durin upgrade` can pull editable installs."))
    report.add(check_optional_extra("fastembed", extra="memory", purpose="vector recall over memory/"))
    report.add(check_optional_extra("lancedb", extra="memory", purpose="vector index storage"))
    report.add(check_cross_encoder_dep())
    report.add(check_optional_extra("mcp", extra="mcp", purpose="MCP server mode"))
    report.add(check_optional_extra("ddgs", extra="web", purpose="DuckDuckGo web_search"))
    report.add(check_optional_extra("readability", extra="web", purpose="web_fetch article extraction"))
    # Audio transcription (spec §8.1): local Whisper extra, TUI mic extra,
    # and cloud API key sanity when a cloud backend is selected.
    report.add(check_stt_installed())
    report.add(check_stt_model_cached())
    report.add(check_voice_extra())
    report.add(check_stt_cloud_keys())
    # Service-level checks: when the user opted into daemon mode or the
    # webui dashboard, verify they're actually up.
    report.add(check_gateway_daemon())
    report.add(check_webui_reachable())
    # Detect new extras and append them to the tracked set, then surface
    # any tracked-but-missing as a warning so the user notices when a
    # reinstall dropped them.
    try:
        update_extras_state()
    except Exception:  # noqa: BLE001
        pass
    report.add(check_extras_drift())
    report.add(check_embedding_model())
    # P11 Fix E (2026-05-30): smoke-test the configured models
    # actually load + work. Goes beyond `check_embedding_model` which
    # only validates the id against the catalog.
    report.add(check_embedding_model_loads())
    report.add(check_cross_encoder_loads())
    report.add(check_memory_summary())
    report.add(check_cache_size())
    if ping:
        report.add(check_provider_reachable())
    if ping_model:
        report.add(check_model_ping())
    return report


def apply_safe_fixes() -> list[str]:
    """Apply the small subset of always-safe fixes. Returns a list of human messages."""
    applied: list[str] = []
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        cfg = None
    if cfg is not None:
        ws = cfg.workspace_path
        if not ws.exists():
            ws.mkdir(parents=True, exist_ok=True)
            applied.append(f"Created workspace at {ws}")
    # Replay schema migration (no-op if up to date).
    from durin.cli.upgrade import migrate_config_file

    if migrate_config_file():
        applied.append("Re-saved config with current schema defaults.")
    return applied


def collect_missing_extras(report: DoctorReport) -> list[str]:
    """Return the unique list of extras whose import failed in this report.

    Reads both the per-extra ``extra`` field (one extra per check, e.g.
    the fastembed/lancedb/mcp probes) and the multi-extra ``extras_list``
    field (one check covering several missing extras, e.g. the
    "previously installed extras" warn that lists discord/oauth/slack).
    """
    seen: list[str] = []
    for r in report.results:
        if r.category != "extras" or r.status != "warn":
            continue
        candidates: list[str] = []
        if r.extra:
            candidates.append(r.extra)
        candidates.extend(r.extras_list)
        for extra in candidates:
            if extra and extra not in seen:
                seen.append(extra)
    return seen


def install_missing_extras(extras: list[str], *, assume_yes: bool = False) -> int:
    """Run the mode-aware install command for ``extras``. Returns the exit code.

    pipx installs use ``--force`` to swap the venv layout, which is mildly
    destructive (anything injected separately gets dropped). We confirm
    before doing it unless ``assume_yes`` is set.
    """
    from durin.cli.upgrade import detect_install_mode, install_hint

    if not extras:
        console.print("[dim]No missing extras to install.[/dim]")
        return 0
    info = detect_install_mode()
    cmd_str = install_hint(extras, mode=info.mode)
    console.print(f"[bold]Detected install mode:[/bold] {info.mode}")
    console.print(f"[bold]Would run:[/bold] [cyan]{cmd_str}[/cyan]")
    if info.mode == "unknown":
        console.print(
            "[red]Cannot auto-install: install mode is unknown.[/red] "
            "Run the command above manually."
        )
        return 1
    if info.mode == "editable":
        console.print(
            "[yellow]Editable mode: run the command above from the source root yourself.[/yellow]"
        )
        return 0
    if not assume_yes:
        if not typer.confirm("Run it?", default=False):
            console.print("[yellow]Aborted.[/yellow]")
            return 1
    # Re-derive the command as a list (instead of shell-quoted string) so we
    # don't shell out and don't need to parse our own quoting.
    bracket = f"[{','.join(extras)}]" if extras else ""
    if info.mode == "pipx":
        from durin.cli.upgrade import PYPI_DIST_NAME, extras_to_packages, pipx_subprocess_env

        # Use `pipx inject` to add the extras' packages to the existing
        # pipx venv. This is non-destructive (no reinstall, no data loss,
        # no config touch) and avoids the broken `pipx install --force`
        # path on the uv backend (silent no-op — see _upgrade_pipx
        # docstring for the full diagnosis). Never replace with
        # `pipx install --extras` or `pipx reinstall` — both would
        # drop existing injections silently. Regression test:
        # tests/cli/test_pipx_subprocess_safety.py.
        env = pipx_subprocess_env()
        pkgs = extras_to_packages(extras)
        if not pkgs:
            console.print(
                "[red]Could not map extras to packages from metadata; "
                "run the install command manually.[/red]"
            )
            return 1
        cmd = ["pipx", "inject", PYPI_DIST_NAME, *pkgs]
        console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
        proc = subprocess.run(cmd, env=env)
        return proc.returncode
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", f"durin-agent{bracket}"]
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    proc = subprocess.run(cmd)
    return proc.returncode


def render_table(report: DoctorReport) -> None:
    by_category: dict[str, list[CheckResult]] = {}
    for r in report.results:
        by_category.setdefault(r.category, []).append(r)
    for category, rows in by_category.items():
        table = Table(title=category, show_header=True, header_style="bold")
        table.add_column("", width=2)
        table.add_column("Check")
        table.add_column("Detail", overflow="fold")
        for r in rows:
            table.add_row(_STATUS_GLYPH[r.status], r.name, r.message)
        console.print(table)

    fixes = [r for r in report.results if r.status in ("warn", "fail") and r.fix]
    if fixes:
        console.print("\n[bold]Suggested fixes:[/bold]")
        for r in fixes:
            # `r.fix` is plain text and may contain `[extra]` literals that
            # Rich would otherwise interpret as markup tags. Use highlight=False
            # to disable Rich parsing entirely for the fix string.
            console.print(f"  [dim]{r.name}:[/dim] ", end="")
            console.out(r.fix)

    counts = report.counts
    summary = f"\n{counts['ok']} ok · {counts['warn']} warn · {counts['fail']} fail"
    color = "green" if report.worst == "ok" else ("yellow" if report.worst == "warn" else "red")
    console.print(f"[{color}]{summary}[/{color}]")


def render_json(report: DoctorReport) -> None:
    payload = {
        "worst": report.worst,
        "counts": report.counts,
        "results": [asdict(r) for r in report.results],
    }
    # Plain `print` — never inject Rich ANSI codes into machine output.
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def run_doctor(
    *,
    ping: bool = False,
    ping_model: bool = False,
    fix: bool = False,
    as_json: bool = False,
    install_missing: bool = False,
    assume_yes: bool = False,
) -> int:
    if fix:
        applied = apply_safe_fixes()
        if applied and not as_json:
            console.print("[bold]Applied fixes:[/bold]")
            for m in applied:
                console.print(f"  [green]✓[/green] {m}")
            console.print("")
    report = run_checks(ping=ping, ping_model=ping_model)
    if install_missing:
        extras = collect_missing_extras(report)
        if extras:
            console.print(f"\n[bold]Missing extras:[/bold] {', '.join(extras)}")
            rc = install_missing_extras(extras, assume_yes=assume_yes)
            if rc != 0:
                return rc
            # `pipx inject` writes new packages into the SAME venv we're
            # running in, but Python has already cached the import
            # negative-lookups for those names. Invalidating the
            # finder caches lets the re-check see what just landed
            # without telling the user to restart.
            importlib.invalidate_caches()
            console.print("\n[bold]Re-checking…[/bold]\n")
            report = run_checks(ping=ping, ping_model=ping_model)
    if as_json:
        render_json(report)
    else:
        render_table(report)
    return 0 if report.worst != "fail" else 1


def register(app: typer.Typer) -> None:
    """Attach the `doctor` command to a Typer app."""

    @app.command("doctor")
    def doctor(
        ping: bool = typer.Option(False, "--ping", help="Test reachability of the active provider's api_base."),
        ping_model: bool = typer.Option(
            False,
            "--ping-model",
            help="Make a real ~3-token call to the configured default model to verify it actually responds (auth, model name, network).",
        ),
        fix: bool = typer.Option(False, "--fix", help="Apply safe fixes (create workspace, re-save config)."),
        install_missing: bool = typer.Option(
            False,
            "--install-missing",
            help="Auto-install any missing optional extras (uses the right command for the detected install mode).",
        ),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts."),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    ) -> None:
        """Diagnose install, config, providers, and runtime state."""
        rc = run_doctor(
            ping=ping,
            ping_model=ping_model,
            fix=fix,
            as_json=as_json,
            install_missing=install_missing,
            assume_yes=yes,
        )
        if rc != 0:
            raise typer.Exit(rc)
