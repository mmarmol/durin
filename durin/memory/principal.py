"""Principal resolution + the pinned hot-context.

The "user" of a message is resolved PER-MESSAGE: owner (config) →
``person:anonymous``. The pinned context (always injected, independent of
retrieval) is the principal's person entity + the ``always_on`` feedback
entities (stance/practice the dream marked always_on). This closes the loop:
authored knowledge is re-injected so the agent actually uses it.

USER.md / MEMORY.md dissolve into this dynamic composition.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.hot_layer import (
    _render_identifiers_line,
    _render_relations_line,
    _render_sources_line,
)
from durin.memory.memory_writer import write_entity

# Cap on the Library awareness catalog pinned every turn. One short line per
# document keeps the agent aware of what it can reach without carrying content.
# Kept conservative because this rides in EVERY prompt; beyond the cap the block
# truncates with a "…and N more" note and the unlisted documents stay reachable
# via `memory_search(scope="library")`. Ranking / topic rollup for large
# libraries is the scaling refinement.
_MAX_LIBRARY_DOCS = 20
_DESC_CHARS = 140
# Cap on the "Covers:" subjects map — the bounded index of what the library is
# about (from documents' distilled topics). Keeps the always-on block bounded
# as the library grows: past the per-document cap, a document is still reachable
# by searching its subject, which this line names.
_MAX_LIBRARY_SUBJECTS = 14

__all__ = [
    "ANONYMOUS",
    "resolve_principal",
    "resolve_owner_principal",
    "ensure_owner",
    "mark_always_on",
    "list_always_on",
    "pinned_refs",
    "resolve_pinned_refs",
    "build_library_awareness",
    "build_pinned_context",
]

ANONYMOUS = "person:anonymous"


def resolve_principal(owner: str | None = None) -> str:
    """Who is the user for this message? owner → anonymous."""
    if owner:
        return owner
    return ANONYMOUS


def _page_path(workspace: Path, ref: str) -> Path:
    type_, _, slug = ref.partition(":")
    return Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md"


def ensure_owner(workspace: Path, owner_ref: str, *, name: str | None = None) -> bool:
    """Cold-start: create a placeholder person entity for the owner if missing.

    Returns True if it created one. The placeholder is dream-authored so the
    agent can enrich it later without precedence conflicts.
    """
    if _page_path(workspace, owner_ref).exists():
        return False
    _type, _, slug = owner_ref.partition(":")
    write_entity(
        workspace, owner_ref,
        [FieldPatch(kind="body_append", value="(auto-created owner placeholder)",
                    author="dream", source_ref="cold_start",
                    at=datetime.now(timezone.utc))],
        create=True, name=name or slug,
    )
    return True


def mark_always_on(workspace: Path, ref: str, on: bool = True) -> None:
    """Mark a feedback entity always_on (dream-owned attribute)."""
    write_entity(
        workspace, ref,
        [FieldPatch(kind="attribute", key="always_on", value=bool(on),
                    author="dream", source_ref="hot_layer_policy",
                    at=datetime.now(timezone.utc))],
        create=True,
    )


def list_always_on(workspace: Path) -> list[str]:
    """Entity refs whose always_on attribute is truthy."""
    root = Path(workspace) / "memory" / "entities"
    out: list[str] = []
    if not root.exists():
        return out
    for md in sorted(root.rglob("*.md")):
        page = EntityPage.from_file(md)
        if page and page.attributes.get("always_on"):
            out.append(f"{md.parent.name}:{md.stem}")
    return out


def resolve_owner_principal(workspace: Path) -> str:
    """The principal the prompt build resolves.

    The one place that reads ``memory.owner`` from config: the prompt build
    and the search dedup must agree on who the principal is, or the dedup
    would exclude a page the pinned block never rendered. A workspace with
    no config file (tests, ad-hoc tools) is a normal state and resolves to
    anonymous.
    """
    try:
        from durin.config.loader import load_config
        owner = getattr(load_config().memory, "owner", None)
    except Exception:  # noqa: BLE001 — no config file is a normal state
        owner = None
    return resolve_principal(owner)


def pinned_refs(
    workspace: Path, principal_ref: str, *, always_on: Sequence[str] | None = None,
) -> frozenset[str]:
    """Every entity ref rendered in the pinned block: the principal's page
    plus the always_on guidance. Readers that show entity pages elsewhere
    in the prompt (the hot layer's canonical block) or in tool output (the
    search dedup) use this set to avoid rendering the same page twice.

    ``always_on`` lets a caller that already walked the entity tree pass the
    result in; omitted, this walks it itself."""
    always = list_always_on(workspace) if always_on is None else always_on
    return frozenset({principal_ref, *always})


# The pinned set only moves when a dream flips an ``always_on`` attribute or
# the owner config changes, and the search dedup asks for it on every search.
# A few seconds of lag there is invisible; re-walking the entity tree per
# search is not.
_PINNED_REFS_TTL_S = 10.0
# Cap on the cache's size: a process serving many workspaces (many tenants,
# or a test suite handing it a fresh tmp_path per test) must not grow this
# without bound.
_PINNED_REFS_CACHE_MAX = 16
_pinned_refs_cache: dict[str, tuple[float, frozenset[str]]] = {}


def resolve_pinned_refs(
    workspace: Path, *, ttl_s: float = _PINNED_REFS_TTL_S,
) -> frozenset[str]:
    """``pinned_refs`` with the principal resolved the way the prompt build
    resolves it: the configured ``memory.owner``, else anonymous. Never
    raises — a workspace without a config file (tests, ad-hoc tools) just
    resolves to anonymous, and any failure degrades to an empty set.

    Memoized per workspace for ``ttl_s`` seconds; pass ``ttl_s=0`` for a
    caller that must observe an ``always_on`` flip immediately. On every
    insert, entries older than ``ttl_s`` are swept and the cache is trimmed
    to ``_PINNED_REFS_CACHE_MAX`` entries (oldest first) so it stays bounded."""
    cache_key = str(workspace)
    if ttl_s > 0:
        hit = _pinned_refs_cache.get(cache_key)
        if hit is not None and (time.monotonic() - hit[0]) < ttl_s:
            return hit[1]
    try:
        refs = pinned_refs(workspace, resolve_owner_principal(workspace))
    except Exception:  # noqa: BLE001 — never break a caller over a pinned lookup
        return frozenset()
    if ttl_s > 0:
        now = time.monotonic()
        for key, (inserted_at, _) in list(_pinned_refs_cache.items()):
            if now - inserted_at >= ttl_s:
                del _pinned_refs_cache[key]
        _pinned_refs_cache[cache_key] = (now, refs)
        while len(_pinned_refs_cache) > _PINNED_REFS_CACHE_MAX:
            oldest_key = min(_pinned_refs_cache, key=lambda k: _pinned_refs_cache[k][0])
            del _pinned_refs_cache[oldest_key]
    return refs


def _load(workspace: Path, ref: str) -> EntityPage | None:
    p = _page_path(workspace, ref)
    return EntityPage.from_file(p) if p.exists() else None


# The principal's page is the one pinned page no budget fits: the extract
# pass keeps appending to it. Cap its body in the prompt and point at
# memory_read_entity for the rest. This is a count of CHARACTERS of body text,
# not tokens, and it is unrelated to `memory.dream.always_on_token_budget` —
# that budget fits the always_on guidance pages (whole, token-counted) and
# never applies to this page.
_PRINCIPAL_BODY_CHARS = 1500


def _render_pinned_block(
    page: EntityPage, *, body_chars: int | None = None, ref: str | None = None,
) -> str:
    """Format one entity page for the always-injected pinned block.

    Renders a superset of what the hot layer's canonical block carries —
    aliases, attributes, relations, legacy identifiers, sources, body — so a
    page that is pinned can be excluded from the canonical block without
    losing anything, and the same exclusion in the search dedup is sound.
    The shared line renderers come from ``hot_layer`` so the two surfaces
    cannot drift apart. ``always_on`` is dropped from the attributes: it is
    the flag that put the page here, not knowledge about it.

    ``body_chars``, when given, caps the body only — every other line is
    untouched. Past the cap the body is cut back to the last space and a
    pointer is appended: naming ``ref`` with ``memory_read_entity`` when
    given, else a bare ellipsis. Left ``None`` (the default) for callers that
    already fit their pages into a token budget of their own (the always_on
    pass); only the principal's page has no such ceiling.
    """
    lines = [f"### {page.name} ({page.type})"]
    if page.aliases:
        lines.append("Aliases: " + ", ".join(page.aliases[:5]) + ".")
    if page.attributes:
        attrs = ", ".join(
            f"{k}: {v}" for k, v in page.attributes.items() if k != "always_on"
        )
        if attrs:
            lines.append(attrs)
    for line in (
        _render_relations_line(page.relations),
        # Legacy v1 emergent field: still rendered so workspaces that have not
        # migrated to v2 attributes keep their identifiers visible.
        _render_identifiers_line(page.extra.get("identifiers") if page.extra else None),
        _render_sources_line(page.derived_from),
    ):
        if line:
            lines.append(line)
    if page.body:
        body = "\n".join(
            ln for ln in page.body.splitlines() if not ln.strip().startswith("<!--")
        ).strip()
        if body_chars is not None and len(body) > body_chars:
            cut = body[:body_chars].rsplit(" ", 1)[0]
            pointer = (
                f" … (truncated; memory_read_entity {ref} for the full page)"
                if ref else " …"
            )
            body = cut + pointer
        if body:
            lines.append(body)
    return "\n".join(lines).strip()


def _doc_descriptor(
    workspace: Path, slug: str, md_path: Path, *, with_abstract: bool = True,
) -> tuple[str, str]:
    """(title, one-line descriptor) for a reference document.

    The descriptor is the distilled outline's abstract when the dream has
    run and ``with_abstract`` is on, otherwise empty (the title alone still
    tells the agent the document exists).
    """
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return slug, ""
    tm = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
    title = tm.group(1).strip().strip('"') if tm else slug
    if not with_abstract:
        return title, ""
    one = ""
    outline = md_path.with_name(f"{slug}.outline.json")
    if outline.exists():
        try:
            abstract = str(json.loads(outline.read_text(encoding="utf-8")).get("abstract") or "").strip()
        except Exception:
            abstract = ""
        if len(abstract) > _DESC_CHARS:
            one = abstract[:_DESC_CHARS].rsplit(" ", 1)[0] + "…"
        else:
            one = abstract
    return title, one


# The subjects map walks and parses EVERY entity page on disk, and the pinned
# block that carries it is rebuilt on every prompt. The map only moves when the
# dream writes entities, so a minute of lag in it is invisible; re-walking the
# tree once per turn is not.
_LIBRARY_SUBJECTS_TTL_S = 60.0
_library_subjects_cache: dict[str, tuple[float, list[str]]] = {}


def _library_subjects(
    workspace: Path, *, cap: int = _MAX_LIBRARY_SUBJECTS,
    ttl_s: float = _LIBRARY_SUBJECTS_TTL_S,
) -> list[str]:
    """The subjects the library covers — its bounded "map".

    Collects the display names of entities the dream distilled *from* a
    reference (a ``derived_from`` link authored by ``dream``). Agent-linked
    entities — a patient whose workup merely cited a paper — are excluded: the
    document is not *about* them, and including them would group the library
    under the wrong things. Ranked by how many documents share each subject
    (broadest first), deduped, capped. Naming the subject-space is what keeps a
    document reachable (search its subject) even past the per-document cap.

    Memoized per workspace for ``ttl_s`` seconds. The ranked list is cached
    uncapped, so any ``cap`` is served from the same entry; pass ``ttl_s=0``
    for a caller that must observe a freshly written entity.
    """
    cache_key = str(workspace)
    if ttl_s > 0:
        hit = _library_subjects_cache.get(cache_key)
        if hit is not None and (time.monotonic() - hit[0]) < ttl_s:
            return hit[1][:cap]
    ents_dir = Path(workspace) / "memory" / "entities"
    if not ents_dir.is_dir():
        return []
    by_subject: dict[str, set[str]] = {}
    for md in sorted(ents_dir.rglob("*.md")):
        page = EntityPage.from_file(md)
        if page is None or not page.derived_from:
            continue
        prov = (page.provenance or {}).get("derived_from")
        prov = prov if isinstance(prov, dict) else {}
        for ref in page.derived_from:
            if (prov.get(ref) or {}).get("author") != "dream":
                continue  # only what a document is ABOUT, not agent-linked refs
            slug = ref.split(":", 1)[1] if ":" in ref else ref
            by_subject.setdefault(page.name, set()).add(slug)
    ranked = sorted(by_subject.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))
    names = [name for name, _docs in ranked]
    if ttl_s > 0:
        _library_subjects_cache[cache_key] = (time.monotonic(), names)
    return names[:cap]


def _library_topics(workspace: Path) -> list[str]:
    """Curated topic labels from the dream's library topic index
    (``memory/references/_topics.json``), in stored order (broadest first).

    This is the clean, stable "map": the dream folds synonyms/translations and
    rolls granular topics up into coherent themes, reusing prior labels so the
    index does not drift. Empty when the dream has not built it yet — the caller
    then falls back to the on-the-fly :func:`_library_subjects` heuristic.
    """
    tpath = Path(workspace) / "memory" / "references" / "_topics.json"
    if not tpath.is_file():
        return []
    try:
        data = json.loads(tpath.read_text(encoding="utf-8"))
    except Exception:
        return []
    topics = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(topics, list):
        return []
    return [
        str(t.get("label")).strip()
        for t in topics
        if isinstance(t, dict) and str(t.get("label") or "").strip()
    ]


def build_library_awareness(
    workspace: Path, *, max_docs: int = _MAX_LIBRARY_DOCS, abstracts: bool = False,
) -> str:
    """A compact, always-on catalog of ingested documents (one line each).

    Gives the agent proactive awareness of what's in the Library without
    carrying any content — the raw documents stay out of default recall, so
    this line-per-document index is how the agent knows a document exists and
    can decide to reach it with ``memory_search(scope="library")`` or a drill.
    Only the listed documents are opened (titles come from their frontmatter);
    the rest are counted. ``max_docs=0`` keeps the header, the count and the
    subject map.
    """
    refs_dir = Path(workspace) / "memory" / "references"
    if not refs_dir.is_dir():
        return ""
    md_files = sorted(refs_dir.glob("*.md"))
    if not md_files:
        return ""
    shown_files = md_files[: max(0, max_docs)]
    docs = [
        _doc_descriptor(workspace, md.stem, md, with_abstract=abstracts)
        for md in shown_files
    ]
    lines = [f"- {t}" + (f" — {d}" if d else "") for t, d in docs]
    more = len(md_files) - len(shown_files)
    if more > 0:
        lines.append(f"- …and {more} more (search its subject to reach it)")
    header = (
        f"## Your document library ({len(md_files)} "
        f"document{'s' if len(md_files) != 1 else ''})"
    )
    note = (
        "These ingested documents are NOT in default recall. Reach one by "
        "searching its subject with `memory_search(scope=\"library\")`, then "
        "drill a `reference:<slug>`; their distilled entities also surface in "
        "normal search carrying a `Sources:` link back to the document."
    )
    # The bounded "subjects map". Preferred source: the dream's curated topic
    # index — clean, stable theme labels — shown always because it is clean.
    # Fallback while the dream has not curated it yet: the on-the-fly
    # distilled-subjects heuristic (granular), shown only once documents fall
    # past the cap, where naming the subject-space keeps a hidden document
    # reachable; below the cap the per-document list already covers everything.
    covers = ""
    topics = _library_topics(workspace)
    if topics:
        covers = f"Covers: {', '.join(topics[:_MAX_LIBRARY_SUBJECTS])}.\n\n"
    elif more > 0:
        subjects = _library_subjects(workspace)
        if subjects:
            covers = f"Covers: {', '.join(subjects)}.\n\n"
    return f"{header}\n\n{note}\n\n{covers}" + "\n".join(lines)


def build_pinned_context(
    workspace: Path, principal_ref: str, *,
    always_on: Sequence[str] | None = None,
    library_max_docs: int = _MAX_LIBRARY_DOCS,
    library_abstracts: bool = False,
) -> str:
    """The always-injected layer: who the user is + always_on feedback +
    a one-line-per-document awareness catalog of the ingested Library.

    ``always_on`` lets a caller that already walked the entity tree pass the
    result in; omitted, this walks it itself.

    ``library_max_docs`` and ``library_abstracts`` go straight to
    :func:`build_library_awareness`: how many documents the catalog lists one
    per line (0 keeps only the header, the count and the subject map), and
    whether each listed line also carries the document's distilled abstract.
    The prompt build fills both from the ``memory.library`` config."""
    parts: list[str] = []
    principal = _load(workspace, principal_ref)
    if principal:
        parts.append(
            "## Who you're talking to\n\n"
            + _render_pinned_block(principal, body_chars=_PRINCIPAL_BODY_CHARS, ref=principal_ref)
        )
    pins: list[str] = []
    for ref in (list_always_on(workspace) if always_on is None else always_on):
        if ref == principal_ref:
            continue
        page = _load(workspace, ref)
        if page:
            pins.append(_render_pinned_block(page))
    if pins:
        parts.append("## Always-on guidance\n\n" + "\n\n".join(pins))
    library = build_library_awareness(
        workspace, max_docs=library_max_docs, abstracts=library_abstracts,
    )
    if library:
        parts.append(library)
    return "\n\n".join(parts)
