"""Persisted, hashed, scoped API token store.

Tokens are stored at ``~/.durin/api_tokens.json`` (or an injected path for
tests).  Only a salted SHA-256 hash is written; the plaintext token is returned
once at issue and never persisted.

The media HMAC secret lives in the same file so it survives a restart; the
file is therefore written mode 0600 (never world-readable), like secrets.json.

Thread-safety mirrors ``durin/pairing/store.py``: a module-level
``threading.Lock`` wraps every op; ``atomic_write_text`` makes writes
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

from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock

_LOCK = threading.Lock()

_EMPTY: dict[str, Any] = {"media_secret": None, "tokens": {}}

# resolve() runs on every authenticated HTTP request; without a cache each
# request pays a flock + full-file parse + per-entry hash scan + fsync'd
# rewrite (last_used_at), which stalls the caller whenever the disk is busy.
# The cache is validated against the file's (mtime_ns, size) on every hit, so
# any writer — this process or another (CLI issue/revoke) — invalidates it
# implicitly; a revoked token is rejected on the very next request.
# (path, sha256(plaintext)) → (st_mtime_ns, st_size, entry-with-token_id)
_RESOLVE_CACHE: dict[tuple[str, str], tuple[int, int, dict[str, Any]]] = {}

# last_used_at is informational (shown in the tokens listing); persisting it
# on every request would defeat the cache. One write per token per window.
_LAST_USED_PERSIST_INTERVAL_S = 60.0


def _cache_key(path: Path, plaintext: str) -> tuple[str, str]:
    """Cache key that never holds the plaintext token in memory."""
    return (str(path), hashlib.sha256(plaintext.encode()).hexdigest())


def _invalidate_resolve_cache(path: Path) -> None:
    p = str(path)
    for key in [k for k in _RESOLVE_CACHE if k[0] == p]:
        del _RESOLVE_CACHE[key]

# Bound store growth: bootstrap mints one token per webui load, so without a
# cap + expiry purge the file would grow without limit (the old in-memory pool
# had the same MAX). Expired tokens are dropped on every issue; if the live set
# still exceeds the cap, the oldest are evicted.
_MAX_TOKENS = 10_000


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
        data.setdefault("hooks_secret", None)
        data.setdefault("tokens", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # mode 0600 — the store holds the media HMAC signing secret and token
        # hashes; it must never be world-readable (mirrors secrets.json).
        atomic_write_text(
            self._path,
            json.dumps(data, indent=2, ensure_ascii=False),
            mode=0o600,
        )

    @staticmethod
    def _purge_expired(data: dict[str, Any], now: float) -> None:
        """Drop tokens whose expiry has passed — bounds store growth."""
        toks = data["tokens"]
        for tid in [
            t
            for t, e in toks.items()
            if e.get("expires_at") is not None and e["expires_at"] < now
        ]:
            del toks[tid]

    @staticmethod
    def _enforce_cap(data: dict[str, Any]) -> None:
        """Keep at most ``_MAX_TOKENS`` live tokens, evicting the oldest."""
        toks = data["tokens"]
        if len(toks) < _MAX_TOKENS:
            return
        oldest = sorted(toks.items(), key=lambda kv: kv[1].get("created_at") or 0.0)
        for tid, _entry in oldest[: len(toks) - _MAX_TOKENS + 1]:
            del toks[tid]

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

        with _LOCK, cross_process_lock(self._path):
            data = self._load()
            self._purge_expired(data, now)
            self._enforce_cap(data)
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
            _invalidate_resolve_cache(self._path)

        return token_id, plaintext

    def resolve(self, plaintext: str) -> dict[str, Any] | None:
        """Validate *plaintext* against stored hashes.

        Returns the entry dict (with ``token_id`` injected) on success, or
        ``None`` if the token is absent, expired, or does not match.

        Hot path: a hit whose file (mtime_ns, size) matches the cached
        snapshot returns without touching the file or any lock beyond
        ``_LOCK``.  ``last_used_at`` is persisted at most once per
        ``_LAST_USED_PERSIST_INTERVAL_S`` per token; the returned entry
        always carries the fresh timestamp.
        """
        now = time.time()
        key = _cache_key(self._path, plaintext)
        try:
            st = self._path.stat()
        except OSError:
            st = None
        with _LOCK:
            cached = _RESOLVE_CACHE.get(key)
            if cached is not None and st is not None:
                mtime_ns, size, entry = cached
                if (st.st_mtime_ns, st.st_size) == (mtime_ns, size):
                    expires_at = entry.get("expires_at")
                    if expires_at is not None and expires_at < now:
                        del _RESOLVE_CACHE[key]
                        return None
                    return {**entry, "last_used_at": now}
        with _LOCK, cross_process_lock(self._path):
            data = self._load()
            for token_id, entry in data["tokens"].items():
                expires_at = entry.get("expires_at")
                if expires_at is not None and expires_at < now:
                    continue
                candidate = _hash_token(entry["salt"], plaintext)
                if hmac.compare_digest(candidate, entry["hash"]):
                    last = entry.get("last_used_at") or 0.0
                    if now - last >= _LAST_USED_PERSIST_INTERVAL_S:
                        entry["last_used_at"] = now
                        self._save(data)
                    try:
                        st = self._path.stat()
                        _RESOLVE_CACHE[key] = (
                            st.st_mtime_ns,
                            st.st_size,
                            {**entry, "token_id": token_id},
                        )
                    except OSError:
                        pass
                    return {**entry, "token_id": token_id, "last_used_at": now}
            _RESOLVE_CACHE.pop(key, None)
        return None

    def revoke(self, token_id: str) -> bool:
        """Remove the token with *token_id*.  Returns ``True`` if it existed."""
        with _LOCK, cross_process_lock(self._path):
            data = self._load()
            if token_id in data["tokens"]:
                del data["tokens"][token_id]
                self._save(data)
                _invalidate_resolve_cache(self._path)
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
        with _LOCK, cross_process_lock(self._path):
            data = self._load()
            if data.get("media_secret"):
                return base64.b64decode(data["media_secret"])
            raw = secrets.token_bytes(32)
            data["media_secret"] = base64.b64encode(raw).decode()
            self._save(data)
            return raw

    def get_or_create_hooks_secret(self) -> str:
        """Return the webhook ingress secret, generating it on first call.

        Unlike the media secret (raw HMAC signing bytes, base64-wrapped),
        this is compared verbatim against the ``X-Durin-Hook-Secret`` header
        on ``POST /api/v1/hooks/{hook}`` and shown directly to operators via
        ``GET /api/v1/loops/hooks-secret``, so it is generated and stored as
        a plain URL-safe token string.
        """
        with _LOCK, cross_process_lock(self._path):
            data = self._load()
            if data.get("hooks_secret"):
                return data["hooks_secret"]
            token = secrets.token_urlsafe(32)
            data["hooks_secret"] = token
            self._save(data)
            return token
