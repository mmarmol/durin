"""Sectioned output renderer for the search pipeline (doc 03 §12).

The final top-K from the search pipeline is grouped by source class
into four sections and rendered with structural markers the LLM
parses:

    === CANONICAL: <uri> (consolidated <ts>) ===     entity pages
    === FRAGMENT: <path> (ts <ts>) ===               episodic + stable
    === SESSION: <session_id>/<turn> (ts <ts>) ===   session summaries
    === INGESTED: <ingest_id>/<chunk> ===            corpus chunks

Sections with zero hits are omitted entirely. Within each section,
hits are ordered by score descending.

Per doc 03 §12.2 the section intro carries descriptive metadata only;
**no valuative language** ("authoritative", "trust this", etc.) —
those have been verified as weak signals.

The per-source cap (doc 03 §12.4) keeps a long ingested doc from
monopolising the top-K with consecutive chunks of itself: at most
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
    """

    uri: str
    type: str  # "entity" | "episodic" | "stable" | "corpus" |
               # "session_summary"
    path: str
    score: float
    ts: str = ""
    snippet: str = ""
    ingest_id: Optional[str] = None
    # Body stays in this dataclass for backward-compat with call
    # sites that consult `hit.body`, but it is no longer populated
    # by the pipeline (audit A4 reverted P2.5). Cold-tier callers
    # default-fall to disk reads via `_enrich_body` — the empty
    # default here is the trigger that activates that path.
    body: str = ""


# Map each source class to a section bucket. Stable entries surface as
# fragments per doc 06 §8 (recent observations beyond the canonical's
# cursor); session summaries get their own section because their
# provenance is different (a conversation, not a memory tool).
_SECTION_FOR_TYPE: dict[str, str] = {
    "entity": "canonical",
    "episodic": "fragment",
    "stable": "fragment",
    "session_summary": "session",
    "corpus": "ingested",
}

_SECTION_ORDER: tuple[str, ...] = (
    "canonical",
    "fragment",
    "session",
    "ingested",
)

_SECTION_INTRO: dict[str, str] = {
    "canonical": (
        "Consolidated entity pages — the main memory; fragments "
        "below amend them with newer information."
    ),
    "fragment": (
        "Episodic and stable entries beyond the canonical cursor. "
        "Reconcile with the canonical above using the timestamps."
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
    """Drop corpus hits beyond the cap per ``ingest_id`` (doc 03 §12.4).

    Preserves order. Only ``type == "corpus"`` hits are subject to the
    cap; everything else passes through untouched.
    """
    seen_per_group: dict[str, int] = {}
    out: list[SectionedHit] = []
    for hit in hits:
        if hit.type != "corpus":
            out.append(hit)
            continue
        key = hit.ingest_id or hit.uri
        count = seen_per_group.get(key, 0)
        if count >= max_per_source:
            continue
        seen_per_group[key] = count + 1
        out.append(hit)
    return out


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
    """One marker block. Body is just the snippet for now; cold-mode
    callers can pass the full body in the snippet field."""
    marker = _marker_for(section, hit)
    body = hit.snippet.strip()
    return f"{marker}\n{body}" if body else marker


def _marker_for(section: str, hit: SectionedHit) -> str:
    if section == "canonical":
        return f"=== CANONICAL: {hit.uri} (consolidated {hit.ts}) ==="
    if section == "fragment":
        return f"=== FRAGMENT: {hit.path} (ts {hit.ts}) ==="
    if section == "session":
        return f"=== SESSION: {hit.uri} (ts {hit.ts}) ==="
    # ingested
    label = hit.ingest_id or "unknown"
    return f"=== INGESTED: {label}/{hit.uri} ==="
