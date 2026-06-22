"""Secret store — see ``docs/11_secrets_design.md``.

Secrets live in ``~/.durin/secrets.json`` (mode ``0600``), separate
from the config tree so config files stay shareable. Config fields
hold a reference (``${secret:NAME}``); the value is resolved lazily at
the point of use and never enters the in-memory ``Config`` object.

Two axes, kept separate:

* ``service`` — *what* the secret is (classification; non-unique).
* ``scope``   — *who* may auto-receive it (``exec`` / ``skill:*`` / …).

Authorization model: the presence of a ``${secret:NAME}`` reference in
config **is** the grant for that field — config-field resolution does
not check ``scope``. ``scope`` gates *auto-injection* (the ``exec``
subprocess env, Phase 2) and the agent ``need_secret`` flow (Phase 3).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock

__all__ = [
    "SecretEntry",
    "SecretStore",
    "SecretError",
    "SecretNotFoundError",
    "is_secret_ref",
    "parse_secret_ref",
    "make_ref",
    "is_valid_secret_name",
    "get_secret_store",
    "resolve_secret",
    "store_secret",
    "SecretRedactor",
    "redact_secrets",
]

# Secret values shorter than this are not redacted — too likely to
# collide with innocuous substrings of normal output.
_MIN_REDACTABLE_LEN = 8

# Secret names double as environment variable names during `exec`
# injection, so they must be env-var-safe.
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# A reference is the WHOLE field value — no partial interpolation.
_REF_RE = re.compile(r"^\$\{secret:([A-Z][A-Z0-9_]*)\}$")


class SecretError(Exception):
    """Base class for secret-store errors."""


class SecretNotFoundError(SecretError):
    """A ``${secret:NAME}`` reference points at a name not in the store."""


class SecretScopeError(SecretError):
    """A consumer requested a secret its ``scope`` does not authorize.

    Reserved for Phase 2/3 (auto-injection, agent flow). Config-field
    resolution never raises this — the reference itself is the grant.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SecretEntry(BaseModel):
    """One stored secret. The ``name`` is the store's map key."""

    value: str
    service: str
    account: str | None = None
    description: str = ""
    scope: list[str] = Field(default_factory=list)
    origin: str = "user"  # user | wizard | migration | agent
    created_at: str = Field(default_factory=_now)


def is_valid_secret_name(name: str) -> bool:
    """True when *name* is a valid (env-var-safe) secret name."""
    return bool(isinstance(name, str) and _NAME_RE.match(name))


def is_secret_ref(value: Any) -> bool:
    """True when *value* is exactly a ``${secret:NAME}`` reference."""
    return isinstance(value, str) and _REF_RE.match(value.strip()) is not None


def parse_secret_ref(value: Any) -> str | None:
    """Return the referenced name, or ``None`` when *value* isn't a ref."""
    if not isinstance(value, str):
        return None
    m = _REF_RE.match(value.strip())
    return m.group(1) if m else None


def make_ref(name: str) -> str:
    """Build the ``${secret:NAME}`` reference string for *name*."""
    return f"${{secret:{name}}}"


def scope_allows(scope: list[str], consumer: str) -> bool:
    """True when *consumer* is permitted by *scope*.

    Exact match, or a ``family:*`` wildcard (``skill:*`` covers
    ``skill:deploy``). Used by Phase-2 auto-injection — NOT by
    config-field resolution.
    """
    if consumer in scope:
        return True
    if ":" in consumer:
        family = consumer.split(":", 1)[0]
        if f"{family}:*" in scope:
            return True
    return False


def mask_secret_hint(secret: str | None) -> str | None:
    """A safe display hint for a secret value: first 4 + last 4 chars (or
    bullets for short/absent values). Never reveals a usable credential — used
    by the secrets API to show a recognizable hint without the value.
    """
    if not secret:
        return None
    if len(secret) <= 8:
        return "••••"
    return f"{secret[:4]}••••{secret[-4:]}"


def _default_secrets_path() -> Path:
    """``secrets.json`` sits next to the active config (testable)."""
    from durin.config.loader import get_config_path

    return get_config_path().parent / "secrets.json"


class SecretStore:
    """Load / mutate / persist ``secrets.json``."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_secrets_path()
        self._entries: dict[str, SecretEntry] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> SecretStore:
        """(Re)read the store from disk. Malformed entries are skipped."""
        self._entries = {}
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
            except (OSError, json.JSONDecodeError):
                raw = {}
            for name, data in (raw.get("secrets") or {}).items():
                if not is_valid_secret_name(name) or not isinstance(data, dict):
                    continue
                try:
                    self._entries[name] = SecretEntry.model_validate(data)
                except Exception:  # noqa: BLE001
                    continue
        self._loaded = True
        return self

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def save(self) -> None:
        """Persist the store, always with mode ``0600`` (plaintext)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_version": 1,
            "secrets": {
                name: entry.model_dump()
                for name, entry in sorted(self._entries.items())
            },
        }
        # Atomic write with mode 0600 — a crash mid-write must never leave a
        # truncated vault (lost credentials), and the file must never be
        # briefly world-readable (mode is forced even on a fresh file).
        atomic_write_text(
            self._path,
            json.dumps(payload, indent=2, ensure_ascii=False),
            mode=0o600,
        )

    # -- queries ----------------------------------------------------------
    def get(self, name: str) -> SecretEntry | None:
        self._ensure()
        return self._entries.get(name)

    def names(self) -> list[str]:
        self._ensure()
        return sorted(self._entries)

    def all(self) -> dict[str, SecretEntry]:
        self._ensure()
        return dict(self._entries)

    def find_by_service(
        self, service: str, account: str | None = None
    ) -> list[str]:
        """Names of secrets matching *service* (and *account* if given)."""
        self._ensure()
        return [
            name
            for name, entry in sorted(self._entries.items())
            if entry.service == service
            and (account is None or entry.account == account)
        ]

    # -- mutations --------------------------------------------------------
    def put(
        self,
        name: str,
        *,
        value: str,
        service: str,
        account: str | None = None,
        description: str = "",
        scope: list[str] | None = None,
        origin: str = "user",
    ) -> None:
        """Create or replace a secret. ``created_at`` survives a replace.

        The load→mutate→save is performed inside ``cross_process_lock`` so
        concurrent callers from different processes cannot lose each other's
        writes. See docs/internals/concurrency.md.
        """
        if not is_valid_secret_name(name):
            raise SecretError(
                f"Invalid secret name '{name}' — must match [A-Z][A-Z0-9_]*"
            )
        with cross_process_lock(self._path):
            self.load()
            existing = self._entries.get(name)
            self._entries[name] = SecretEntry(
                value=value,
                service=service,
                account=account,
                description=description,
                scope=list(scope or []),
                origin=origin,
                created_at=existing.created_at if existing else _now(),
            )
            self.save()

    def remove(self, name: str) -> bool:
        """Delete a secret. Returns True when something was removed.

        The load→mutate→save is performed inside ``cross_process_lock`` so
        concurrent callers from different processes cannot lose each other's
        writes. See docs/internals/concurrency.md.
        """
        with cross_process_lock(self._path):
            self.load()
            removed = self._entries.pop(name, None) is not None
            if removed:
                self.save()
        return removed

    def set_scope(self, name: str, scope: list[str]) -> bool:
        """Replace a secret's ``scope`` in memory only. Returns False when unknown.

        Mutates the in-memory entry but does NOT persist; callers needing
        durability must use :meth:`set_scope_locked`.
        """
        self._ensure()
        entry = self._entries.get(name)
        if entry is None:
            return False
        entry.scope = list(scope)
        return True

    def set_scope_locked(self, name: str, scope: list[str]) -> bool:
        """Replace a secret's ``scope`` under a cross-process lock. Returns False when unknown.

        The load→mutate→save is performed inside ``cross_process_lock`` so
        concurrent callers from different processes cannot lose each other's
        writes. See docs/internals/concurrency.md.
        """
        with cross_process_lock(self._path):
            self.load()
            entry = self._entries.get(name)
            if entry is None:
                return False
            entry.scope = list(scope)
            self.save()
        return True

    def grant_consumer_locked(self, name: str, consumer: str) -> bool | None:
        """Add *consumer* to a secret's scope under a cross-process lock.

        Returns True when the tag was added, False when it was already present,
        and None when the secret is unknown. The read-compute-write is entirely
        inside ``cross_process_lock`` so concurrent grants from different
        processes both survive. See docs/internals/concurrency.md.
        """
        with cross_process_lock(self._path):
            self.load()
            entry = self._entries.get(name)
            if entry is None:
                return None
            if consumer in entry.scope:
                return False
            entry.scope = [*entry.scope, consumer]
            self.save()
        return True

    def revoke_consumer_locked(self, name: str, consumer: str) -> bool | None:
        """Remove *consumer* from a secret's scope under a cross-process lock.

        Returns True when the tag was removed, False when it was not present,
        and None when the secret is unknown. The read-compute-write is entirely
        inside ``cross_process_lock`` so concurrent revokes from different
        processes both survive. See docs/internals/concurrency.md.
        """
        with cross_process_lock(self._path):
            self.load()
            entry = self._entries.get(name)
            if entry is None:
                return None
            if consumer not in entry.scope:
                return False
            entry.scope = [tag for tag in entry.scope if tag != consumer]
            self.save()
        return True

    # -- resolution -------------------------------------------------------
    def resolve(self, value: Any) -> Any:
        """Resolve a config value to plaintext.

        ``${secret:NAME}`` → the stored value; anything else (literal,
        ``None``) is returned unchanged. Does NOT check ``scope`` — a
        reference written into config is itself the authorization.
        """
        name = parse_secret_ref(value)
        if name is None:
            return value
        self._ensure()
        entry = self._entries.get(name)
        if entry is None:
            raise SecretNotFoundError(
                f"Config references secret '{name}' but it is not in "
                f"the store ({self._path}). Add it with `durin secret set {name}`."
            )
        return entry.value

    def collect_for(self, consumer: str) -> dict[str, str]:
        """All ``{name: value}`` whose ``scope`` authorizes *consumer*.

        Used by Phase-2 auto-injection (e.g. the ``exec`` subprocess
        env). Config-field resolution uses :meth:`resolve` instead.
        """
        self._ensure()
        return {
            name: entry.value
            for name, entry in self._entries.items()
            if scope_allows(entry.scope, consumer)
        }


_STORE: SecretStore | None = None


def get_secret_store(*, reload: bool = False) -> SecretStore:
    """Return the process-wide store (cached).

    Pass ``reload=True`` after a mutation, or when the active config
    path changed (tests), to rebuild from disk.
    """
    global _STORE
    if _STORE is None or reload:
        _STORE = SecretStore().load()
    return _STORE


def resolve_secret(value: Any) -> Any:
    """Resolve a config value via the process-wide store.

    A ``${secret:NAME}`` reference becomes the stored plaintext; a
    literal (or ``None``) is returned untouched. Raises
    :class:`SecretNotFoundError` for a dangling reference.

    This is the function every config consumer calls right before
    using a credential — keeping the resolved plaintext out of the
    ``Config`` object, logs, and telemetry.
    """
    if not is_secret_ref(value):
        return value
    return get_secret_store().resolve(value)


def store_secret(
    name: str,
    value: str,
    *,
    service: str,
    scope: list[str],
    description: str = "",
    origin: str = "user",
) -> str:
    """Store *value* in the secret store; return its ``${secret:}`` reference.

    *name* is sanitized to an env-var-safe secret name. The plaintext
    lands only in ``secrets.json`` (mode 0600). Shared by the onboard
    wizard and the web dashboard so both write references, never
    plaintext, into config.
    """
    import re

    secret_name = re.sub(r"[^A-Z0-9_]", "_", name.upper())
    if not secret_name or not secret_name[0].isalpha():
        secret_name = "S_" + secret_name
    store = SecretStore()
    store.put(
        secret_name,
        value=value,
        service=service,
        description=description,
        scope=list(scope),
        origin=origin,
    )
    get_secret_store(reload=True)
    return make_ref(secret_name)


# -- A5: pattern-based redaction ---------------------------------------------
# The value-based redactor only knows secrets in the store. Credentials
# surfaced via `exec.allowed_env_keys` (ambient os.environ) — or otherwise
# echoed into output — are invisible to it. This second layer catches
# credential-*shaped* strings by format, regardless of the store. Modelled on
# hermes (`agent/redact.py`) and openclaw (`src/logging/redact.ts`): vendor
# prefixes + conservative key=value heuristics. Format-based, language-
# agnostic (no NLP token lists). Pattern matches mask to a bare `«redacted»`.

_PATTERN_MARKER = "«redacted»"

# Vendor-prefixed credentials — high confidence, low false-positive.
_VENDOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),                       # OpenAI / Anthropic / OpenRouter
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{59,}"),              # GitHub fine-grained PAT
    re.compile(r"\bgh[opsur]_[A-Za-z0-9]{36,}"),                # GitHub PAT / OAuth / refresh
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),              # Slack
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),                   # Google API key
    re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"),                  # Google OAuth access token
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),               # AWS access key id
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}"),  # Stripe
    re.compile(r"\bSG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),  # SendGrid
    re.compile(r"\bnpm_[A-Za-z0-9]{36}"),                       # npm
    re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}"),                   # PyPI
    re.compile(                                                 # JWT (three base64url segments)
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    ),
)

# PEM private-key blocks (multi-line).
_PEM_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# `Authorization: Bearer <token>` — keep the scheme word, mask the credential.
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{12,})")

# env-style assignment with an UPPER_SNAKE key naming a credential (the exact
# shape an `env` dump or `allowed_env_keys` leak takes). Case-sensitive on the
# key so common lowercase words ("Total tokens: 123") are not matched.
_ENV_KV_PATTERN = re.compile(
    r"\b([A-Z][A-Z0-9_]*"
    r"(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|ACCESS_?KEY"
    r"|PRIVATE_?KEY|CLIENT_?SECRET|CREDENTIALS?|AUTH)"
    r"[A-Z0-9_]*)(\s*[:=]\s*)([^\s\"']{8,})"
)

# Quoted JSON-style `"apiKey": "value"` — mask the value, keep the key.
_JSON_KV_PATTERN = re.compile(
    r"(?i)([\"']\s*[a-z0-9_]*"
    r"(?:api_?key|secret|token|password|passwd|passphrase|access_?key"
    r"|private_?key|client_?secret|credentials?)"
    r"[a-z0-9_]*\s*[\"']\s*:\s*[\"'])([^\"']{8,})"
)


def _apply_patterns(text: str) -> str:
    """Mask credential-shaped substrings by format (A5)."""
    text = _PEM_PATTERN.sub(_PATTERN_MARKER, text)
    for pat in _VENDOR_PATTERNS:
        text = pat.sub(_PATTERN_MARKER, text)
    text = _BEARER_PATTERN.sub(lambda m: m.group(1) + _PATTERN_MARKER, text)
    text = _ENV_KV_PATTERN.sub(lambda m: m.group(1) + m.group(2) + _PATTERN_MARKER, text)
    text = _JSON_KV_PATTERN.sub(lambda m: m.group(1) + _PATTERN_MARKER, text)
    return text


class SecretRedactor:
    """Replaces secret values with ``«redacted…»`` markers.

    Two layers. **Value-based** (always): exact stored values become
    ``«redacted:NAME»``; values shorter than :data:`_MIN_REDACTABLE_LEN`
    are ignored. **Pattern-based** (opt-in via ``patterns=True``, A5):
    credential-shaped substrings become ``«redacted»`` regardless of the
    store — this is what covers ambient ``allowed_env_keys`` values.
    """

    def __init__(self, secrets: dict[str, str], *, patterns: bool = False) -> None:
        self._patterns = patterns
        # Longest values first so a value that is a substring of another
        # is not masked prematurely.
        self._items: list[tuple[str, str]] = sorted(
            (
                (value, name)
                for name, value in secrets.items()
                if isinstance(value, str) and len(value) >= _MIN_REDACTABLE_LEN
            ),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )

    @property
    def active(self) -> bool:
        return bool(self._items) or self._patterns

    def redact_text(self, text: str) -> str:
        for value, name in self._items:
            if value in text:
                text = text.replace(value, f"«redacted:{name}»")
        if self._patterns:
            text = _apply_patterns(text)
        return text

    def redact(self, content: Any) -> Any:
        """Redact a tool-result content — a string or a list of blocks."""
        if not self._items and not self._patterns:
            return content
        if isinstance(content, str):
            return self.redact_text(content)
        if isinstance(content, list):
            out: list[Any] = []
            for block in content:
                if isinstance(block, dict):
                    b = dict(block)
                    for key in ("text", "content"):
                        if isinstance(b.get(key), str):
                            b[key] = self.redact_text(b[key])
                    out.append(b)
                elif isinstance(block, str):
                    out.append(self.redact_text(block))
                else:
                    out.append(block)
            return out
        return content


def build_redactor() -> SecretRedactor:
    """Build a redactor from the store, with the pattern layer (A5) on."""
    store = get_secret_store()
    return SecretRedactor(
        {name: entry.value for name, entry in store.all().items()},
        patterns=True,
    )


def redact_secrets(content: Any) -> Any:
    """Convenience: redact *content* against the current store."""
    return build_redactor().redact(content)


def migrate_plaintext_provider_keys(config_path: Path | None = None) -> list[str]:
    """Move plaintext provider ``apiKey`` values on disk into the store.

    For each ``providers.<name>.apiKey`` that is a non-empty literal
    (not already a ``${secret:…}`` reference): a store entry is created
    (``service``/``scope`` = ``provider:<name>``, ``origin=migration``)
    and the config field is rewritten to the reference.

    Idempotent — values already shaped as references are skipped.
    Backs the config up first. Returns the secret names created.
    """
    import json as _json
    import re as _re

    from durin.config.loader import (
        _is_split_layout,
        _split_dir,
        backup_config,
        get_config_path,
    )

    path = config_path or get_config_path()

    providers_file: Path | None
    mono: dict[str, Any] | None
    if _is_split_layout(path):
        providers_file = _split_dir(path) / "providers.json"
        if not providers_file.exists():
            return []
        try:
            providers = _json.loads(providers_file.read_text(encoding="utf-8") or "{}")
        except (OSError, _json.JSONDecodeError):
            return []
        mono = None
    elif path.exists():
        try:
            mono = _json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, _json.JSONDecodeError):
            return []
        if not isinstance(mono, dict) or mono.get("_layout") == "split":
            return []
        providers = mono.get("providers")
        providers_file = None
    else:
        return []

    if not isinstance(providers, dict):
        return []

    # (provider_name, field_name, plaintext_value)
    pending: list[tuple[str, str, str]] = []
    for pname, pcfg in providers.items():
        if not isinstance(pcfg, dict):
            continue
        for field in ("apiKey", "api_key"):
            val = pcfg.get(field)
            if isinstance(val, str) and val.strip() and not is_secret_ref(val):
                pending.append((pname, field, val))
                break

    if not pending:
        return []

    backup_config(path)
    store = SecretStore()
    created: list[str] = []
    for pname, field, val in pending:
        base = _re.sub(r"[^A-Z0-9_]", "_", pname.upper())
        if not base or not base[0].isalpha():
            base = "P_" + base
        sec_name = f"{base}_API_KEY"
        store.put(
            sec_name,
            value=val,
            service=f"provider:{pname}",
            description=f"{pname} API key",
            scope=[f"provider:{pname}"],
            origin="migration",
        )
        providers[pname][field] = make_ref(sec_name)
        created.append(sec_name)

    if providers_file is not None:
        atomic_write_text(
            providers_file, _json.dumps(providers, indent=2, ensure_ascii=False)
        )
    elif mono is not None:
        mono["providers"] = providers
        atomic_write_text(path, _json.dumps(mono, indent=2, ensure_ascii=False))

    get_secret_store(reload=True)
    return created
