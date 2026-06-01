"""Shared `=== KIND: ... ===` marker construction (audit G7, 2026-05-28).

Two renderers in this package wrap canonical / fragment / session /
ingested content in the marker convention documented in
``docs/architecture/memory/06_prompts_and_instructions.md`` §8.3:

- ``durin.memory.hot_layer`` — eager pre-injection into every agent
  prompt; renders structured EntityPage objects.
- ``durin.memory.sectioned_output`` — lazy search-result rendering
  for ``memory_search``; renders SectionedHit rows.

Audit F4 (2026-05-28) unified the body-rendering paths for search;
audit G6 (2026-05-28) reaffirmed that the two renderers stay
intentionally separate — the hot layer carries the full entity-page
structure (attributes/relations/identifiers/body excerpt) while the
search renderer carries the search-hit shape (summary > body >
snippet, optional entities tail). Their inner content is
fundamentally different and merging them would produce a
two-renderers-in-a-trenchcoat module.

What they DO share is the marker convention. Pre-G7 each module
built the marker strings independently — two places to drift. G7
ships this helper so the marker format has a single source of
truth without forcing the renderers to merge.

Functions return the header string only. Callers append the body
content and the ``end_marker`` line themselves.
"""

from __future__ import annotations

__all__ = [
    "canonical_marker",
    "end_marker",
    "fragment_marker",
    "ingested_marker",
    "session_marker",
]


def _compose_qualifiers(*parts: str) -> str:
    """Join non-empty qualifiers inside a single trailing parenthesis.

    H5 (audit 2026-05-29) introduces an optional ``completeness``
    qualifier (``"complete"`` / ``"preview N/M"``) that lives in the
    same parens as the existing ``ts`` / ``consolidated`` qualifier.
    Composing them here keeps the marker shape predictable and lets
    each marker helper express its specific qualifiers without
    re-implementing the join.
    """
    kept = [p for p in parts if p]
    if not kept:
        return ""
    return " (" + ", ".join(kept) + ")"


def canonical_marker(
    ref: str, *, ts: str = "", completeness: str = "",
) -> str:
    """Header for a canonical entity-page block.

    With ``ts`` set: ``=== CANONICAL: <ref> (consolidated <ts>) ===``
    With ``ts`` empty: ``=== CANONICAL: <ref> (canonical entity page) ===``

    Entity pages do not carry a ``valid_from`` so the descriptive
    ``(canonical entity page)`` variant is what `memory_search`
    emits in practice; the hot layer always passes a `consolidated_ts`
    (the file mtime of the entity page) so it always gets the
    timestamped variant.

    H5 (audit 2026-05-29): ``completeness`` (optional) is appended to
    the trailing parens — ``"complete"`` when the rendered body is the
    whole entry, ``"preview N/M"`` when more is available via drill.
    """
    primary = f"consolidated {ts}" if ts else "canonical entity page"
    return f"=== CANONICAL: {ref}{_compose_qualifiers(primary, completeness)} ==="


def fragment_marker(
    path: str, *, ts: str = "", completeness: str = "",
) -> str:
    """Header for a fragment block (episodic / stable / post-cursor).

    With ``ts`` set: ``=== FRAGMENT: <path> (ts <ts>) ===``
    With ``ts`` empty: ``=== FRAGMENT: <path> ===``

    H5: ``completeness`` (optional) appends to the trailing parens.
    """
    primary = f"ts {ts}" if ts else ""
    return f"=== FRAGMENT: {path}{_compose_qualifiers(primary, completeness)} ==="


def session_marker(
    uri: str, *, ts: str = "", completeness: str = "",
) -> str:
    """Header for a session block (summary or turn match).

    With ``ts`` set: ``=== SESSION: <uri> (ts <ts>) ===``
    With ``ts`` empty: ``=== SESSION: <uri> ===``

    H5: ``completeness`` (optional) appends to the trailing parens.
    """
    primary = f"ts {ts}" if ts else ""
    return f"=== SESSION: {uri}{_compose_qualifiers(primary, completeness)} ==="


def ingested_marker(
    ingest_id: str | None, uri: str, *, completeness: str = "",
) -> str:
    """Header for an ingested-content block.

    Format: ``=== INGESTED: <ingest_id>/<uri> ===``. When the
    ``ingest_id`` is not known the helper falls back to ``unknown``
    so the LLM still sees a structured marker line.

    H5: ``completeness`` (optional) appends to the trailing parens.
    """
    label = ingest_id or "unknown"
    return f"=== INGESTED: {label}/{uri}{_compose_qualifiers(completeness)} ==="


def end_marker(kind: str) -> str:
    """Closing line for any block: ``=== END <KIND> ===``."""
    return f"=== END {kind.upper()} ==="
