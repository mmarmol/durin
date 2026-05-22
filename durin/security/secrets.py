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
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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
        # O_CREAT with 0600 so the file is never briefly world-readable.
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        # Re-assert mode in case the file pre-existed with looser perms.
        os.chmod(self._path, 0o600)

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
        """Create or replace a secret. ``created_at`` survives a replace."""
        if not is_valid_secret_name(name):
            raise SecretError(
                f"Invalid secret name '{name}' — must match [A-Z][A-Z0-9_]*"
            )
        self._ensure()
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

    def remove(self, name: str) -> bool:
        """Delete a secret. Returns True when something was removed."""
        self._ensure()
        return self._entries.pop(name, None) is not None

    def set_scope(self, name: str, scope: list[str]) -> bool:
        """Replace a secret's ``scope``. Returns False when unknown."""
        self._ensure()
        entry = self._entries.get(name)
        if entry is None:
            return False
        entry.scope = list(scope)
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
    store = SecretStore().load()
    store.put(
        secret_name,
        value=value,
        service=service,
        description=description,
        scope=list(scope),
        origin=origin,
    )
    store.save()
    get_secret_store(reload=True)
    return make_ref(secret_name)


class SecretRedactor:
    """Replaces stored secret values with ``«redacted:NAME»`` markers.

    Built from the store; used to keep secret values out of anything
    the model or the user sees (tool results, output). Values shorter
    than :data:`_MIN_REDACTABLE_LEN` are ignored.
    """

    def __init__(self, secrets: dict[str, str]) -> None:
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
        return bool(self._items)

    def redact_text(self, text: str) -> str:
        for value, name in self._items:
            if value in text:
                text = text.replace(value, f"«redacted:{name}»")
        return text

    def redact(self, content: Any) -> Any:
        """Redact a tool-result content — a string or a list of blocks."""
        if not self._items:
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
    """Build a redactor from every value in the process-wide store."""
    store = get_secret_store()
    return SecretRedactor(
        {name: entry.value for name, entry in store.all().items()}
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
    store = SecretStore().load()
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
    store.save()

    if providers_file is not None:
        providers_file.write_text(
            _json.dumps(providers, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    elif mono is not None:
        mono["providers"] = providers
        path.write_text(
            _json.dumps(mono, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    get_secret_store(reload=True)
    return created
