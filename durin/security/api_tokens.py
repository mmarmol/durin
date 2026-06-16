"""Persisted, hashed, scoped API token store.

Tokens are stored at ``~/.durin/api_tokens.json`` (or an injected path for
tests).  Only a salted SHA-256 hash is written; the plaintext token is returned
once at issue and never persisted.

The media HMAC secret lives in the same file so it survives a restart.

Thread-safety mirrors ``durin/pairing/store.py``: a module-level
``threading.Lock`` wraps every op; ``_write_text_atomic`` makes writes
crash-safe.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from durin.utils.helpers import _write_text_atomic

_LOCK = threading.Lock()

_EMPTY: dict[str, Any] = {"media_secret": None, "tokens": {}}


def _hash_token(salt_hex: str, plaintext: str) -> str:
    """Return SHA-256 hex of ``salt_bytes + plaintext.encode()``."""
    salt = bytes.fromhex(salt_hex)
    return hashlib.sha256(salt + plaintext.encode()).hexdigest()


class ApiTokenStore:
    """Thread-safe, file-backed store for API tokens and the media HMAC secret.

    Args:
        path: Override the default ``get_data_dir()/api_tokens.json``.
              Pass a ``tmp_path``-derived path in tests.
    """

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from durin.config.paths import get_data_dir

            path = get_data_dir() / "api_tokens.json"
        self._path = path

    # ------------------------------------------------------------------
    # Internal load / save (always called under _LOCK)
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {"media_secret": None, "tokens": {}}
        except (json.JSONDecodeError, OSError):
            return {"media_secret": None, "tokens": {}}
        data.setdefault("media_secret", None)
        data.setdefault("tokens", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(self._path, json.dumps(data, indent=2, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def issue(
        self,
        scopes: list[str],
        *,
        label: str = "",
        ttl_s: float | None = None,
    ) -> tuple[str, str]:
        """Mint a new token.

        Returns ``(token_id, plaintext_token)``.  The plaintext is shown ONCE
        and never stored — only the salted SHA-256 hash is persisted.
        """
        plaintext = f"nbwt_{secrets.token_urlsafe(32)}"
        token_id = secrets.token_hex(8)
        salt_hex = secrets.token_hex(16)
        hash_hex = _hash_token(salt_hex, plaintext)
        now = time.time()
        expires_at = now + ttl_s if ttl_s is not None else None

        with _LOCK:
            data = self._load()
            data["tokens"][token_id] = {
                "hash": hash_hex,
                "salt": salt_hex,
                "scopes": list(scopes),
                "label": label,
                "kind": "remote",
                "created_at": now,
                "expires_at": expires_at,
                "last_used_at": None,
            }
            self._save(data)

        return token_id, plaintext

    def resolve(self, plaintext: str) -> dict[str, Any] | None:
        """Validate *plaintext* against stored hashes.

        Returns the entry dict (with ``token_id`` injected) on success, or
        ``None`` if the token is absent, expired, or does not match.  On
        success ``last_used_at`` is updated and persisted.
        """
        now = time.time()
        with _LOCK:
            data = self._load()
            for token_id, entry in data["tokens"].items():
                expires_at = entry.get("expires_at")
                if expires_at is not None and expires_at < now:
                    continue
                candidate = _hash_token(entry["salt"], plaintext)
                if hmac.compare_digest(candidate, entry["hash"]):
                    entry["last_used_at"] = now
                    self._save(data)
                    return {**entry, "token_id": token_id}
        return None

    def revoke(self, token_id: str) -> bool:
        """Remove the token with *token_id*.  Returns ``True`` if it existed."""
        with _LOCK:
            data = self._load()
            if token_id in data["tokens"]:
                del data["tokens"][token_id]
                self._save(data)
                return True
        return False

    def list_tokens(self) -> list[dict[str, Any]]:
        """Return metadata for all tokens.

        Hash and salt are never included — callers receive only id, label,
        scopes, kind, created_at, expires_at, last_used_at.
        """
        with _LOCK:
            data = self._load()
        result = []
        for token_id, entry in data["tokens"].items():
            result.append(
                {
                    "token_id": token_id,
                    "label": entry.get("label", ""),
                    "scopes": entry.get("scopes", []),
                    "kind": entry.get("kind", "remote"),
                    "created_at": entry.get("created_at"),
                    "expires_at": entry.get("expires_at"),
                    "last_used_at": entry.get("last_used_at"),
                }
            )
        return result

    def get_or_create_media_secret(self) -> bytes:
        """Return the 32-byte media HMAC secret, generating it on first call.

        The secret is stored as base64 in the JSON file so it survives a
        process restart.
        """
        with _LOCK:
            data = self._load()
            if data.get("media_secret"):
                return base64.b64decode(data["media_secret"])
            raw = secrets.token_bytes(32)
            data["media_secret"] = base64.b64encode(raw).decode()
            self._save(data)
            return raw
