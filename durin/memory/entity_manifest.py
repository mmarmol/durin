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

__all__ = ["build_entity_manifest", "full_body_entries"]


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


def _pages_by_type(root: Path, types: Sequence[str]) -> list[tuple[str, EntityPage]]:
    """Every page of ``types`` as ``(ref, page)``: the types in the order
    given, each type's pages in slug order."""
    out: list[tuple[str, EntityPage]] = []
    for type_ in types:
        tdir = root / type_
        if not tdir.is_dir():
            continue
        for md in sorted(tdir.glob("*.md")):
            page = EntityPage.from_file(md)
            if page is not None:
                out.append((f"{type_}:{md.stem}", page))
    return out


def full_body_entries(
    workspace: Path,
    *,
    types: Sequence[str] | None = None,
    refs: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[tuple[str, str]]:
    """``(ref, entry)`` pairs, each entry carrying the page's full body, for a
    caller that may replace a body and so has to know exactly which pages it
    showed. ``types`` walks those types (see ``_pages_by_type``) up to
    ``limit``; ``refs`` renders exactly those pages, skipping any that does
    not exist."""
    root = Path(workspace) / "memory" / "entities"
    pages: list[tuple[str, EntityPage]] = []
    if refs is not None:
        for ref in refs:
            type_, _, slug = ref.partition(":")
            path = root / type_ / f"{slug}.md"
            page = EntityPage.from_file(path) if type_ and slug and path.is_file() else None
            if page is not None:
                pages.append((ref, page))
    elif types:
        pages = _pages_by_type(root, types)
    if limit is not None:
        pages = pages[:limit]
    return [(ref, _full_entry(ref, page)) for ref, page in pages]


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

    `types` mode: enumerate the entities of those types by walking
    memory/entities/<type>/ directories directly — the types in the order
    given, each in slug order — and keep the first `limit`. Suited for small
    canonical sets (feedback, stance, practice) where listing is cheap; a set
    larger than `limit` is cut, so the later types can go unlisted.

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
        for ref, page in _pages_by_type(root, types)[:limit]:
            lines.append(_full_entry(ref, page) if full_body else _line(ref, page))
        return "\n".join(lines)

    if query and query.strip():
        from durin.memory.scope import ScopePredicate
        from durin.memory.search_pipeline import run_search_pipeline
        result = run_search_pipeline(
            Path(workspace), query, vector_index=vector_index, limit=limit,
            scope=ScopePredicate.entity_pages(),
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
            # Chunk/section uris can also carry ":" with path-ish slugs
            # ("memory:reference/…#11") — those are not entity refs, and a
            # missing page must filter out, never abort the caller's pass.
            if not type_ or not slug or "/" in slug or "#" in slug:
                continue
            try:
                page = EntityPage.from_file(root / type_ / f"{slug}.md")
            except OSError:
                continue
            if page is not None:
                seen.add(ref)
                lines.append(_line(ref, page))
        return "\n".join(lines[:limit])

    return ""
