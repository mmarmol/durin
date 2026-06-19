"""GitHub augmentation for MCP discovery: resolve repos to stars/owner/topics,
classify first-party servers, and cache results. All network access is injectable
so unit tests run offline. GraphQL requires a token; without one, enrichment is a
no-op and the quality gate is disabled (see mcp_catalog_cache / search).
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

_GH_RE = re.compile(r"github\.com[/:]([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?:/|$)")


def parse_repo_url(url: str) -> tuple[str, str] | None:
    """Return (owner, name) for a github.com URL, else None."""
    if not url:
        return None
    m = _GH_RE.search(url)
    if not m:
        return None
    owner, name = m.group(1), m.group(2)
    if owner in (".", "..") or name in (".", ".."):
        return None
    return owner, name


def _default_gh_runner() -> str | None:
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tok = (out.stdout or "").strip()
    return tok or None


def _default_secret_getter(name: str) -> str | None:
    """Resolve a durin secret by NAME — copies skill_resolve._github_token's logic."""
    from durin.security.secrets import resolve_secret

    try:
        return str(resolve_secret(f"${{secret:{name}}}") or "") or None
    except Exception:  # noqa: BLE001 — missing secret / store issue → anonymous
        return None


def resolve_token(
    *, env: dict | None = None, gh_runner=None, secret_getter=None, secret_name: str = ""
) -> str | None:
    """Resolve a GitHub token: gh CLI → env → durin secret. None if unavailable."""
    env = os.environ if env is None else env
    gh_runner = _default_gh_runner if gh_runner is None else gh_runner
    secret_getter = _default_secret_getter if secret_getter is None else secret_getter
    if tok := (gh_runner() or None):
        return tok
    for key in ("GITHUB_TOKEN", "DURIN_GITHUB_TOKEN"):
        if env.get(key):
            return env[key]
    if secret_getter and secret_name:
        if tok := secret_getter(secret_name):
            return tok
    return None
