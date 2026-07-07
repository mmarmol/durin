"""Compact "what we already know" manifest for seeding extraction prompts.

The dream's discover and learnings passes pass this to the LLM so it reuses an
existing entity ref instead of minting a new slug for a fact it already holds.
Two modes: enumerate a small canonical set by type (feedback/stance/practice),
or retrieve the top-k relevant entities for a large set (person/place/topic).
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from durin.memory.entity_page import EntityPage

__all__ = ["build_entity_manifest"]


def _one_line(page: EntityPage) -> str:
    """First non-empty non-comment body line, else a summary of attributes."""
    if page.body:
        for ln in page.body.splitlines():
            if ln.strip() and not ln.strip().startswith("<!--"):
                return ln.strip()[:160]
    if page.attributes:
        return ", ".join(f"{k}: {v}" for k, v in page.attributes.items()
                         if k != "always_on")[:160]
    return ""


def _line(ref: str, page: EntityPage) -> str:
    desc = _one_line(page)
    return f"- {ref} — {page.name}: {desc}" if desc else f"- {ref} — {page.name}"


def _full_entry(ref: str, page: EntityPage) -> str:
    """Ref + name + the entity's FULL body — for a caller that may REPLACE that
    body in place (learnings), so it refines the existing text instead of
    overwriting content it never saw."""
    body = (page.body or "").strip()
    head = f"- {ref} — {page.name}"
    return f"{head}:\n{body}" if body else head


def build_entity_manifest(
    workspace: Path,
    *,
    types: Sequence[str] | None = None,
    query: str | None = None,
    limit: int = 20,
    vector_index: object | None = None,
    full_body: bool = False,
) -> str:
    """Return a compact manifest of existing entities for prompt seeding.

    `types` mode: enumerate ALL entities of those types by walking
    memory/entities/<type>/ directories directly. Suited for small
    canonical sets (feedback, stance, practice) where exhaustive listing
    is cheap and completeness matters more than relevance ranking.

    `query` mode: retrieve the top-k relevant entities via the search
    pipeline. Suited for large sets (person, place, topic) where
    exhaustive listing is impractical. Requires the search index to be
    populated; hits whose uri is a valid entity ref (<type>:<slug>) are
    resolved to their page on disk.

    When both are given, `types` mode wins. Returns empty string when
    nothing matches.
    """
    root = Path(workspace) / "memory" / "entities"
    lines: list[str] = []

    if types:
        for type_ in types:
            tdir = root / type_
            if not tdir.is_dir():
                continue
            for md in sorted(tdir.glob("*.md")):
                page = EntityPage.from_file(md)
                if page is not None:
                    ref = f"{type_}:{md.stem}"
                    lines.append(_full_entry(ref, page) if full_body else _line(ref, page))
        return "\n".join(lines[:limit])

    if query and query.strip():
        from durin.memory.search_pipeline import run_search_pipeline
        result = run_search_pipeline(
            Path(workspace), query, vector_index=vector_index, limit=limit,
        )
        seen: set[str] = set()
        for hit in result.hits:
            # SectionedHit.uri holds the entity ref for entity pages
            # (e.g. "person:deborah") — exactly the ref shape we need.
            # Non-entity hits have uri shapes like "memory/episodic/<id>";
            # the ":" check and entity-file existence test filter those out.
            ref = hit.uri
            if not isinstance(ref, str) or ":" not in ref or ref in seen:
                continue
            type_, _, slug = ref.partition(":")
            page = EntityPage.from_file(root / type_ / f"{slug}.md")
            if page is not None:
                seen.add(ref)
                lines.append(_line(ref, page))
        return "\n".join(lines[:limit])

    return ""
