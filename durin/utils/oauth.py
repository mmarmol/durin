"""Shared helpers for inspecting OAuth-token presence on disk.

Both ``durin status`` and ``durin doctor`` need to answer the same
question: "do I actually have a valid-looking OAuth token for this
provider, or am I just seeing the spec entry?" Centralising the
lookup keeps the two surfaces honest with each other and lets us
extend the search to new storage layouts in one place.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

__all__ = ["token_storage_paths", "any_token_present", "should_use_device_code"]


def _legacy_paths(provider_name: str) -> Iterable[Path]:
    """Best-effort fallback candidates kept for backwards compatibility.

    These predate the ``oauth-cli-kit`` storage layout and may still
    exist on long-lived installs. Checking them avoids false negatives
    if the user logged in before the storage migration.
    """
    home = Path.home()
    yield home / ".durin" / "oauth" / f"{provider_name}.json"
    yield home / f".{provider_name}" / "auth.json"


def _kit_paths(provider_name: str) -> Iterable[Path]:
    """Canonical paths used by ``oauth-cli-kit``'s ``FileTokenStorage``.

    The kit is an optional dependency; if it isn't installed we
    silently skip its candidates. The user can't have logged in
    without it being present at the time, so there'd be nothing
    interesting in those paths anyway.
    """
    try:
        from oauth_cli_kit.providers import (
            GITHUB_COPILOT_PROVIDER,
            OPENAI_CODEX_PROVIDER,
        )
        from oauth_cli_kit.storage import FileTokenStorage
    except ImportError:
        return ()

    spec_map = {
        "openai_codex": OPENAI_CODEX_PROVIDER,
        "github_copilot": GITHUB_COPILOT_PROVIDER,
    }
    spec = spec_map.get(provider_name)
    if spec is None:
        return ()
    try:
        storage = FileTokenStorage(token_filename=spec.token_filename)
        return (storage.get_token_path(),)
    except Exception:  # noqa: BLE001
        return ()


def token_storage_paths(provider_name: str) -> list[Path]:
    """All paths that *might* hold a token for ``provider_name``."""
    return [*_kit_paths(provider_name), *_legacy_paths(provider_name)]


def any_token_present(provider_name: str) -> bool:
    """True iff at least one storage path actually exists on disk."""
    return any(p.exists() for p in token_storage_paths(provider_name))


def should_use_device_code() -> bool:
    """True when loopback PKCE is unlikely to work (remote/headless shell).

    Loopback needs the user's browser and the local callback server on the same
    machine. Over SSH or without a GUI that does not hold, so device-code is the
    safe default.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    if sys.platform in ("darwin", "win32"):
        return False  # GUI desktop assumed
    return not os.environ.get("DISPLAY")  # Linux: GUI only if a display is set
