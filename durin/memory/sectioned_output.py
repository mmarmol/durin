"""Sectioned output renderer for the search pipeline.

The final top-K from the search pipeline is grouped by source class
into four sections and rendered with structural markers the LLM
parses:

    === CANONICAL: <uri> (consolidated <ts>) ===     entity pages
    === FRAGMENT: <path> (ts <ts>) ===               episodic + stable
    === SESSION: <session_id>/<turn> (ts <ts>) ===   session summaries
    === INGESTED: <ingest_id>/<chunk> ===            corpus chunks

Sections with zero hits are omitted entirely. Within each section,
hits are ordered by score descending.

The section intro carries descriptive metadata only; **no valuative
language** ("authoritative", "trust this", etc.) — those have been
verified as weak signals.

The per-source cap keeps a long ingested doc from monopolising the
top-K with consecutive chunks of itself: at most
``max_per_source`` corpus hits per ``ingest_id``. Other classes are
not capped (their clustering is triangulation, not duplication).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

__all__ = [
    "DEFAULT_MAX_PER_SOURCE",
    "SectionedHit",
    "apply_per_source_cap",
    "render_sectioned",
]


DEFAULT_MAX_PER_SOURCE: int = 3


@dataclass(frozen=True)
class SectionedHit:
    """One result row carried into the renderer.

    Built upstream by the pipeline after fusion + entity-aware rerank.
    The renderer only consumes these fields; it does not enrich them.

    Added ``summary`` and ``entities`` so the
    renderer has the same data Result.render_block used to reach. The
    body preference inside the block is `summary > body > snippet`;
    the entities tail tags fragments so the LLM can drill to canonical.
    """

    uri: str
    type: str  # "skill" | "entity" | "episodic" | "stable" |
               # "corpus" | "session_summary"
    path: str
    score: float
    ts: str = ""
    snippet: str = ""
    ingest_id: Optional[str] = None
    # Body stays in this dataclass for backward-compat with call
    # sites that consult `hit.body`, but it is no longer populated
    # by the pipeline. Cold-tier callers
    # default-fall to disk reads via `_enrich_body` — the empty
    # default here is the trigger that activates that path.
    body: str = ""
    summary: str = ""
    entities: tuple[str, ...] = ()
    # Total source body length so the renderer can compute the per-hit
    # completeness qualifier. ``0`` means
    # unknown (lexical-only hits, legacy callers) — the renderer
    # then omits the signal entirely (backward-compat marker shape).
    body_length: int = 0


# Map each source class to a section bucket. Stable entries surface as
# fragments (recent observations beyond the canonical's cursor); session
# summaries get their own section because their provenance is different
# (a conversation, not a memory tool).
_SECTION_FOR_TYPE: dict[str, str] = {
    "skill": "skill",
    "entity": "canonical",
    "episodic": "fragment",
    "stable": "fragment",
    "session_summary": "session",
    # Raw per-turn session rows (FTS-indexed sessions). Distinct from
    # `session_summary` (the compactor's rolling summary) so stats and
    # the type prior can tell raw transcript from distilled content.
    "session": "session",
    "corpus": "ingested",
    # Ingested reference documents (the Library). Chunks share the
    # INGESTED section with legacy corpus chunks.
    "reference": "ingested",
}

# Skills lead: a matching procedure is a "playbook" to execute, so it
# belongs before the facts the agent reconciles. The three dicts above
# and below are COUPLED — `by_section` is pre-seeded from this order
# and `_SECTION_INTRO` is indexed unguarded, so every name here must
# also have a `_SECTION_INTRO` entry or rendering KeyErrors.
_SECTION_ORDER: tuple[str, ...] = (
    "skill",
    "canonical",
    "fragment",
    "session",
    "ingested",
)

_SECTION_INTRO: dict[str, str] = {
    "skill": (
        "Procedures matching the query — follow these steps to "
        "execute the task; they are instructions, not facts to cite."
    ),
    "canonical": (
        "Consolidated entity pages — the main memory; fragments "
        "below amend them with newer information."
    ),
    "fragment": (
        "Recent episodic and stable entries — raw memories that may carry "
        "newer info than the canonical above. Reconcile using the timestamps."
    ),
    "session": (
        "Session summaries and turn-level matches from prior "
        "conversations."
    ),
    "ingested": (
        "Chunks from ingested documents matching the query."
    ),
}


def apply_per_source_cap(
    hits: Iterable[SectionedHit],
    *,
    max_per_source: int = DEFAULT_MAX_PER_SOURCE,
) -> list[SectionedHit]:
    """Drop ingested-document hits beyond the cap per source document.

    Preserves order. Only ingested chunks (``type`` ``"corpus"`` or
    ``"reference"``) are subject to the cap, keyed by their source
    document so one long document can't monopolise the results;
    everything else passes through untouched.
    """
    seen_per_group: dict[str, int] = {}
    out: list[SectionedHit] = []
    for hit in hits:
        if hit.type not in ("corpus", "reference"):
            out.append(hit)
            continue
        key = _source_group_key(hit)
        count = seen_per_group.get(key, 0)
        if count >= max_per_source:
            continue
        seen_per_group[key] = count + 1
        out.append(hit)
    return out


def _source_group_key(hit: SectionedHit) -> str:
    """Group key for the per-source cap: the source document, not the chunk.

    Reference chunks address ``memory/reference/reference:<slug>#<idx>``;
    strip the ``#<idx>`` so every chunk of one document shares a key. Corpus
    hits carry an ``ingest_id`` that already groups them.
    """
    if hit.ingest_id:
        return hit.ingest_id
    if hit.type == "reference" and "#" in hit.uri:
        return hit.uri.rsplit("#", 1)[0]
    return hit.uri


def render_sectioned(hits: Iterable[SectionedHit]) -> str:
    """Render the sectioned output as a single string.

    Sections appear in canonical → fragment → session → ingested
    order. Empty sections are omitted; if no hits exist the function
    returns ``""``.
    """
    by_section: dict[str, list[SectionedHit]] = {
        s: [] for s in _SECTION_ORDER
    }
    for hit in hits:
        section = _SECTION_FOR_TYPE.get(hit.type, "fragment")
        by_section[section].append(hit)
    for section in by_section:
        by_section[section].sort(key=lambda h: h.score, reverse=True)

    parts: list[str] = []
    for section in _SECTION_ORDER:
        section_hits = by_section[section]
        if not section_hits:
            continue
        parts.append(f"## {section.title()}\n\n{_SECTION_INTRO[section]}")
        for hit in section_hits:
            parts.append(_render_block(section, hit))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _render_block(section: str, hit: SectionedHit) -> str:
    """One marker block. Brought to feature parity with the (now
    retired) `Result.render_block`:

    - Body preference: ``summary > body > snippet``.
    - END marker (`=== END KIND ===`) closes each block.
    - Entities tail (`Entities: ...`) for non-canonical hits so the
      LLM can drill to the canonical page.
    - Canonical header uses ``(canonical entity page)`` when no ts,
      ``(consolidated <ts>)`` when ts is present.

    The marker gets a completeness qualifier when the source body
    length is known (``hit.body_length > 0``).
    ``complete`` means the rendered text IS the whole body — drilling
    returns the same content; ``preview N/M`` means more is available.
    """
    rendered_body = (hit.summary or hit.body or hit.snippet or "").strip()
    marker = _marker_for(
        section, hit, completeness=_completeness_for(hit, rendered_body),
    )
    parts = [marker]
    if rendered_body:
        parts.append(rendered_body)
    if section not in ("canonical", "skill") and hit.entities:
        parts.append(f"Entities: {', '.join(hit.entities)}")
    from durin.memory.section_markers import end_marker
    parts.append(end_marker(section))
    return "\n".join(parts)


def _completeness_for(hit: SectionedHit, rendered_body: str) -> str:
    """Compute the H5 completeness qualifier for a rendered block.

    Returns ``"complete"`` when the rendered text covers the entire
    source body, ``"preview N/M"`` when more is available via drill,
    or ``""`` when the body length is unknown (backward-compat path
    for legacy / lexical-only hits).
    """
    if hit.body_length <= 0:
        return ""
    rendered_len = len(rendered_body)
    if rendered_len >= hit.body_length:
        return "complete"
    return f"preview {rendered_len}/{hit.body_length}"


def _marker_for(
    section: str, hit: SectionedHit, *, completeness: str = "",
) -> str:
    # Delegate to the shared `section_markers` helper so the marker
    # format has one source of truth across both this renderer and
    # `hot_layer`. The body composition stays here — only the header
    # strings are shared.
    # Pass through the completeness qualifier.
    from durin.memory.section_markers import (
        canonical_marker,
        fragment_marker,
        ingested_marker,
        session_marker,
        skill_marker,
    )
    if section == "skill":
        return skill_marker(hit.uri, completeness=completeness)
    if section == "canonical":
        return canonical_marker(
            hit.uri, ts=hit.ts, completeness=completeness,
        )
    if section == "fragment":
        return fragment_marker(
            hit.path, ts=hit.ts, completeness=completeness,
        )
    if section == "session":
        return session_marker(
            hit.uri, ts=hit.ts, completeness=completeness,
        )
    return ingested_marker(
        hit.ingest_id, hit.uri, completeness=completeness,
    )
