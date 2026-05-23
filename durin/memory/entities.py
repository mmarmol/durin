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
``docs/18_entity_centric_plan.md`` §4 — these are hints for the
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
from dataclasses import dataclass

__all__ = [
    "ENTITY_REF_PATTERN",
    "SUGGESTED_TYPES",
    "InvalidEntityRefError",
    "ParsedEntityRef",
    "is_valid_entity_ref",
    "parse_entity_ref",
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
