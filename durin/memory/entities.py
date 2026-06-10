"""Typed entity references for memory entries.

Each entity reference has the form ``<type>:<value>``:

- ``<type>`` lowercase, ``[a-z][a-z0-9_]*``, length >=1
- ``:`` separator (only the first one separates)
- ``<value>`` non-empty, can contain anything except the leading
  whitespace

Examples valid:    ``person:marcelo``, ``project:durin``,
``topic:autocompaction``, ``artifact:settings.py``.

The vocabulary of types is **open**: any well-formed type is accepted.
:data:`SUGGESTED_TYPES` lists the 8 broad types from
``docs/architecture/memory/01_data_and_entities.md`` §3.7 — these are hints for the
consolidator/dream prompt, not an enforced enum. The LLM can introduce
new types when content demands it (Phase 0.3 confirmed ``agent:``,
``org:`` emerge naturally).

Validation policy (per ``docs/14_typed_entities_proposal.md`` §3.2):

- ``memory_store`` write-path: **strict**. Invalid refs → error
  returned to the model so it can rewrite.
- ``consolidator_tags`` read-path: **lenient**. Invalid refs are
  dropped with a log warning; the entry survives.
- Direct Python paths: raise ``InvalidEntityRefError``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from unidecode import unidecode

__all__ = [
    "ENTITY_REF_PATTERN",
    "SUGGESTED_TYPES",
    "InvalidEntityRefError",
    "ParsedEntityRef",
    "is_valid_entity_ref",
    "parse_entity_ref",
    "resolve_slug_collision",
    "slugify_name",
    "split_valid_invalid",
    "normalize_entity_ref",
]


# Regex per doc 14 §3.2.  Anchored to whole string.
ENTITY_REF_PATTERN = re.compile(r"^[a-z][a-z0-9_]*:[^\s].*$")


# 8 broad types suggested by doc 18 §4 — Tulving/CoALA-grounded.
# Open vocabulary: NOT enforced; types outside this set are welcome.
SUGGESTED_TYPES: frozenset[str] = frozenset(
    [
        "person",
        "place",
        "project",
        "topic",
        "event",
        "artifact",
        "stance",
        "practice",
    ]
)


class InvalidEntityRefError(ValueError):
    """Raised when an entity reference string fails the format check."""


@dataclass(frozen=True)
class ParsedEntityRef:
    """A parsed ``<type>:<value>`` reference."""

    type: str
    value: str

    def __str__(self) -> str:  # pragma: no cover — trivial
        return f"{self.type}:{self.value}"


def is_valid_entity_ref(ref: str) -> bool:
    """Return True iff *ref* matches ``<type>:<value>`` per the spec."""
    if not isinstance(ref, str):
        return False
    return bool(ENTITY_REF_PATTERN.match(ref))


def parse_entity_ref(ref: str) -> ParsedEntityRef:
    """Parse ``<type>:<value>`` into its components.

    Raises ``InvalidEntityRefError`` if *ref* doesn't match the spec.
    Only the **first** ``:`` separates — values can themselves contain
    colons (e.g., ``file:path/with:colons.md``).
    """
    if not is_valid_entity_ref(ref):
        raise InvalidEntityRefError(
            f"invalid entity reference {ref!r}: must match <type>:<value> "
            f"where type is lowercase [a-z][a-z0-9_]* and value is non-empty"
        )
    type_, _, value = ref.partition(":")
    return ParsedEntityRef(type=type_, value=value)


def split_valid_invalid(refs: list[str]) -> tuple[list[str], list[str]]:
    """Partition *refs* into (valid, invalid) lists, preserving order.

    Used by lenient read paths (consolidator_tags) that want to drop bad
    refs without rejecting the whole entry.
    """
    valid: list[str] = []
    invalid: list[str] = []
    for ref in refs:
        if is_valid_entity_ref(ref):
            valid.append(ref)
        else:
            invalid.append(ref)
    return valid, invalid


# ---------------------------------------------------------------------------
# Slug normalization (doc memory §4.5)
# ---------------------------------------------------------------------------

_SLUG_MAX_LEN = 64
_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify_name(name: str) -> str:
    """Derive a canonical entity slug from a free-form name.

    Pipeline per ``docs/architecture/memory/01_data_and_entities.md`` §4.5:
      1. Unicode NFC normalize
      2. Transliterate non-Latin scripts (CJK, Cyrillic, Arabic, ...)
         to Latin ASCII via :mod:`unidecode`.
      3. Lowercase.
      4. Replace runs of non-alphanumerics with single underscore.
      5. Strip leading / trailing underscores.
      6. Truncate to 64 chars (re-stripping trailing ``_`` if the cut
         lands inside a word boundary).

    Returns ``"unnamed"`` if the pipeline yields an empty string (caller
    decides whether to reject or accept this placeholder).
    """
    if not isinstance(name, str):
        return "unnamed"
    nfc = unicodedata.normalize("NFC", name)
    latinised = unidecode(nfc)
    lowered = latinised.lower()
    slug = _SLUG_NON_ALNUM.sub("_", lowered).strip("_")
    if len(slug) > _SLUG_MAX_LEN:
        slug = slug[:_SLUG_MAX_LEN].rstrip("_")
    return slug or "unnamed"


def resolve_slug_collision(
    workspace: Path,
    type_: str,
    base_slug: str,
) -> str:
    """Return ``base_slug`` or ``<base_slug>_<N>`` so the result doesn't
    clash with an existing entity of the same type.

    Checks both live (``memory/entities/<type>/<slug>.md``) and archived
    (``memory/archive/entities/<type>/<slug>.md``) — reviving an
    archived slug would silently re-attach a stale identity to a new
    page.
    """
    workspace = Path(workspace)
    live_dir = workspace / "memory" / "entities" / type_
    archived_dir = workspace / "memory" / "archive" / "entities" / type_

    def _taken(slug: str) -> bool:
        return (
            (live_dir / f"{slug}.md").exists()
            or (archived_dir / f"{slug}.md").exists()
        )

    if not _taken(base_slug):
        return base_slug
    suffix = 2
    while _taken(f"{base_slug}_{suffix}"):
        suffix += 1
    return f"{base_slug}_{suffix}"


def normalize_entity_ref(ref: str) -> str:
    """Lowercase the *type* portion; leave value as-is.

    Useful when accepting input that's already valid in shape but with
    capitalized type (e.g., ``Person:Marcelo`` → ``person:Marcelo``).
    For inputs that don't match the basic shape, raises
    ``InvalidEntityRefError``.
    """
    if not isinstance(ref, str) or ":" not in ref:
        raise InvalidEntityRefError(
            f"cannot normalize {ref!r}: missing ':' separator"
        )
    type_, _, value = ref.partition(":")
    normalized = f"{type_.lower()}:{value}"
    if not is_valid_entity_ref(normalized):
        raise InvalidEntityRefError(
            f"normalized form {normalized!r} still doesn't match spec"
        )
    return normalized
