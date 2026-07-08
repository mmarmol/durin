"""Single source of truth for durin's GitHub API token.

Precedence: ``gh`` CLI (``gh auth token``) -> environment (``GITHUB_TOKEN`` /
``DURIN_GITHUB_TOKEN``) -> durin's shared secret store. Returns ``""`` (anonymous)
when nothing is configured and never raises: GitHub access degrades to
unauthenticated rather than breaking skills, MCP discovery, or the GitHub MCP
server.

One credential, three consumers. This replaces the per-feature token lookups that
skills (``skill_resolve``) and MCP discovery (``mcp_github``) each did on their own.
The device-flow connect writes the shared ``GITHUB_OAUTH`` secret;
``legacy_secret_names`` lets a previously-configured per-feature secret keep working
until the operator migrates.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence

# Written by the device-flow connect; read by every general GitHub consumer.
SHARED_SECRET_NAME = "GITHUB_OAUTH"

# durin's own GitHub OAuth App (device flow -> public client id, no client secret,
# so it is safe to ship in this open-source repo).
DURIN_GITHUB_CLIENT_ID = "Ov23lixcqd7ZjiTogO4h"

_ENV_KEYS = ("GITHUB_TOKEN", "DURIN_GITHUB_TOKEN")


def _default_gh_runner() -> str | None:
    """``gh auth token``, or None if gh is absent / not logged in."""
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tok = (out.stdout or "").strip()
    return tok or None


def _default_secret_getter(name: str) -> str | None:
    from durin.security.secrets import resolve_secret

    try:
        return str(resolve_secret(f"${{secret:{name}}}") or "") or None
    except Exception:  # noqa: BLE001 - missing secret / store issue -> anonymous
        return None


def resolve_github_token(
    *,
    env: dict | None = None,
    gh_runner: Callable[[], str | None] | None = None,
    secret_getter: Callable[[str], str | None] | None = None,
    legacy_secret_names: Sequence[str] = (),
) -> str:
    """Resolve the GitHub token: gh CLI -> env -> shared secret -> legacy secrets.

    Returns ``""`` for anonymous. Every source is best-effort - a flaky ``gh`` or an
    unreadable secret store degrades to the next source, never raises.
    """
    env = os.environ if env is None else env
    gh_runner = _default_gh_runner if gh_runner is None else gh_runner
    secret_getter = _default_secret_getter if secret_getter is None else secret_getter

    try:
        if tok := (gh_runner() or None):
            return str(tok)
    except Exception:  # noqa: BLE001 - a flaky gh must not break resolution
        pass

    for key in _ENV_KEYS:
        if env.get(key):
            return str(env[key])

    for name in (SHARED_SECRET_NAME, *legacy_secret_names):
        try:
            if tok := secret_getter(name):
                return str(tok)
        except Exception:  # noqa: BLE001 - unreadable store -> try next / anonymous
            continue

    return ""
