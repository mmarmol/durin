"""Auto-install of optional feature extras on activation.

A feature maps to a pip extra declared in pyproject `[project.optional-dependencies]`.
When a feature is activated but its extra is missing, `ensure_extra` installs it
(gated by `config.install.auto_install_extras`). See
docs/superpowers/specs/2026-06-06-auto-install-extras-design.md.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import logging
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeatureExtra:
    feature: str         # stable key used by call-sites
    extra: str           # pyproject extra name -> durin-agent[<extra>]
    module: str          # importable module proving the extra is present
    needs_restart: bool  # dep only takes effect after a gateway restart
    approx_size: str     # human download estimate for the confirm dialog
    label: str           # human feature name


# Phase-1 probe modules (ddgs, sentence_transformers) are verified. Phase-2
# entries' modules are best-effort and re-confirmed when each is wired.
REGISTRY: dict[str, FeatureExtra] = {
    "web_search": FeatureExtra("web_search", "web", "ddgs", False, "~5 MB", "Web search"),
    "cross_encoder": FeatureExtra("cross_encoder", "cross-encoder", "sentence_transformers", True, "~1 GB", "Cross-encoder reranker"),
    "mcp": FeatureExtra("mcp", "mcp", "mcp", True, "~10 MB", "MCP servers"),
    "slack": FeatureExtra("slack", "slack", "slack_sdk", True, "~10 MB", "Slack channel"),
    "discord": FeatureExtra("discord", "discord", "discord", True, "~10 MB", "Discord channel"),
    "memory_vector": FeatureExtra("memory_vector", "memory", "fastembed", True, "~400 MB", "Vector memory"),
    "local_models": FeatureExtra("local_models", "local", "llama_cpp", True, "~200 MB", "Local models"),
    "oauth": FeatureExtra("oauth", "oauth", "oauth_cli_kit", False, "~5 MB", "OAuth providers"),
}

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass
class EnsureResult:
    status: str               # "present" | "installed" | "failed" | "disabled"
    feature: str
    needs_restart: bool = False
    message: str = ""


def _module_present(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def _extra_specs(extra: str) -> list[str]:
    """Package specs for durin-agent's <extra>, from installed metadata.

    Avoids duplicating pyproject pins. A requirement line looks like
    ``sentence-transformers>=3.0,<6.0; extra == "cross-encoder"``.
    """
    specs: list[str] = []
    for req in importlib.metadata.requires("durin-agent") or []:
        if f'extra == "{extra}"' in req:
            specs.append(req.split(";", 1)[0].strip())
    return specs


def _installer_cmd(specs: list[str]) -> list[str] | None:
    if not specs:
        return None
    if _module_present("pip"):
        return [sys.executable, "-m", "pip", "install", *specs]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, *specs]
    return None


def _lock_for(feature: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(feature, threading.Lock())


def ensure_extra(feature: str, *, config) -> EnsureResult:
    """Ensure ``feature``'s pip extra is importable, installing it if allowed."""
    fe = REGISTRY[feature]
    if _module_present(fe.module):
        return EnsureResult("present", feature, fe.needs_restart)
    install_cfg = getattr(config, "install", None)
    if install_cfg is not None and not getattr(install_cfg, "auto_install_extras", True):
        return EnsureResult(
            "disabled", feature, fe.needs_restart,
            f"Run: pip install durin-agent[{fe.extra}]",
        )
    with _lock_for(feature):
        if _module_present(fe.module):
            return EnsureResult("present", feature, fe.needs_restart)
        specs = _extra_specs(fe.extra)
        cmd = _installer_cmd(specs)
        if not cmd:
            return EnsureResult(
                "failed", feature, fe.needs_restart,
                "No installer (pip or uv) found on PATH.",
            )
        logger.info("extras: installing %s -> %s", feature, specs)
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            return EnsureResult("failed", feature, fe.needs_restart, (e.stderr or "")[-800:])
        importlib.invalidate_caches()
        if not _module_present(fe.module):
            return EnsureResult(
                "failed", feature, fe.needs_restart,
                "Installed but the module is still not importable.",
            )
        _post_install(feature)
        return EnsureResult("installed", feature, fe.needs_restart)


def _post_install(feature: str) -> None:
    """Per-feature in-process cleanup so the dep takes effect without restart
    where possible."""
    if feature == "cross_encoder":
        try:
            from durin.memory import cross_encoder
            cross_encoder.reset_global()
        except Exception:  # pragma: no cover - best effort
            logger.debug("extras: cross_encoder reset_global failed", exc_info=True)


def ensure_or_note(feature: str, *, config) -> EnsureResult:
    """Runtime convenience around ``ensure_extra``: install if missing and log a
    one-line outcome. Callers inspect ``.status`` (retry if "present"/"installed"
    and not needs_restart) and ``.needs_restart`` (tell the user to restart)."""
    res = ensure_extra(feature, config=config)
    if res.status == "installed" and res.needs_restart:
        logger.info(
            "extras: installed %s — restart the gateway to activate it", feature
        )
    elif res.status == "failed":
        logger.warning("extras: could not install %s: %s", feature, res.message)
    return res
