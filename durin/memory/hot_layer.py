"""Hot layer — the always-loaded memory section of the stable prompt tier.

Phase 1.9 of the memory subsystem (renderer), refreshed in Phase 1.5
(canonical spec: ``docs/internals/memory/06_prompts_and_instructions.md``
§8). The hot layer is what the agent carries in every prompt without
any tool call: identity essentials, canonical entity pages (the "main
memory"), recent tagged fragments (two-track model, N3: fragments are not
consolidated into pages, so they surface by recency), top headlines and a
de-duplicated entity name list. By design it changes at most once per Dream pass; between
passes it is read-only so the upstream provider's prompt cache stays
warm across many turns.

The renderer reads from disk on every prompt build (cheap walk + YAML
parse, <5ms typical). Sections that fail to assemble degrade silently
and emit ``memory.hot_layer.failure`` telemetry per §8.7 — the agent
still works, just without the broken section.

Per doc 06 §8.3, canonical pages and recent fragments are wrapped in
``=== CANONICAL: <uri> (consolidated <ts>) ===`` and
``=== FRAGMENT: <path> (ts <ts>) ===`` markers so the LLM reconciles
contradictions at read time using the timestamps embedded in the
markers. Same convention as the compaction
``=== ARCHIVED SUMMARY ===`` block (logbook 2026-05-19) and
``memory_search``'s result rendering.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from durin.memory.entity_page import EntityPage
from durin.memory.paths import MEMORY_CLASSES, walk_class
from durin.memory.storage import load_entry
from durin.telemetry.logger import current_telemetry

__all__ = ["HotLayer", "read_hot_layer"]

# Budgets per docs/internals/memory/06_prompts_and_instructions.md §8.2.
# Total ~1900 tokens — still cache-friendly between dreams.
_IDENTITY_BUDGET_CHARS = 800    # ~200 tokens
_CANONICAL_BUDGET_CHARS = 2400  # ~600 tokens — N entity pages
_FRAGMENTS_BUDGET_CHARS = 1200  # ~300 tokens — recent tagged entries
_HEADLINES_BUDGET_CHARS = 1200  # ~300 tokens — legacy class entries
_ENTITIES_BUDGET_CHARS = 600    # ~150 tokens
_MAX_CANONICAL = 12
_MAX_FRAGMENTS = 8
_MAX_HEADLINES = 12
_MAX_ENTITIES = 50

# Per-page body cap inside the canonical block. Keeps a single huge
# page from consuming the whole canonical budget.
_CANONICAL_BODY_PER_PAGE = 600
# Per-fragment body cap for the same reason.
_FRAGMENT_BODY_PER_PAGE = 400

# Classes that surface as "fragments" in the hot layer per §8.4.
# Corpus and pending are intentionally excluded.
_FRAGMENT_CLASSES: tuple[str, ...] = ("episodic", "stable")


class HotLayer(NamedTuple):
    identity: str
    canonical_blocks: list[str]
    fragment_blocks: list[str]
    headlines: list[str]
    entities: list[str]

    def render(self) -> str:
        """Render the hot layer as markdown for the stable prompt tier.

        Section order follows §8.3: identity → canonical (main memory) →
        fragments (recent, by recency) → headlines (legacy entries) →
        entity list. The LLM sees the canonical first, marked as
        authoritative; fragments come next with their timestamp so the
        model can reconcile temporal contradictions.
        """
        parts: list[str] = []
        if self.identity.strip():
            parts.append(f"## Memory: Identity\n\n{self.identity}")
        if self.canonical_blocks:
            body = "\n\n".join(self.canonical_blocks)
            parts.append(
                "## Memory: Canonical pages\n\n"
                "These are the authoritative records — fragments below "
                "amend them with newer information.\n\n"
                + body
            )
        if self.fragment_blocks:
            body = "\n\n".join(self.fragment_blocks)
            parts.append(
                "## Memory: Recent fragments\n\n"
                "Recent episodic entries — raw memories that may carry newer "
                "info than the canonical above. Reconcile using the "
                "timestamps.\n\n"
                + body
            )
        if self.headlines:
            bullets = "\n".join(f"- {h}" for h in self.headlines)
            parts.append(f"## Memory: Key Points\n\n{bullets}")
        if self.entities:
            csv = ", ".join(self.entities)
            parts.append(f"## Memory: Known Entities\n\n{csv}")
        return "\n\n".join(parts)


def read_hot_layer(workspace: Path) -> HotLayer:
    """Assemble the hot layer for a workspace.

    Each section is wrapped in its own try/except: any failure emits
    ``memory.hot_layer.failure`` telemetry per §8.7 and degrades the
    section to empty so the prompt still builds.
    """
    try:
        canonicals = _read_canonical_blocks(workspace)
    except Exception as exc:  # pragma: no cover - defensive
        _emit_failure("canonical_blocks", exc)
        canonicals = []

    try:
        fragments = _read_fragment_blocks(workspace)
    except Exception as exc:  # pragma: no cover - defensive
        _emit_failure("fragment_blocks", exc)
        fragments = []

    try:
        identity = _read_identity(workspace)
    except Exception as exc:  # pragma: no cover - defensive
        _emit_failure("identity", exc)
        identity = ""

    try:
        headlines = _read_top_headlines(workspace)
    except Exception as exc:  # pragma: no cover - defensive
        _emit_failure("headlines", exc)
        headlines = []

    try:
        entities = _read_entity_list(workspace)
    except Exception as exc:  # pragma: no cover - defensive
        _emit_failure("entities", exc)
        entities = []

    return HotLayer(
        identity=identity,
        canonical_blocks=canonicals,
        fragment_blocks=fragments,
        headlines=headlines,
        entities=entities,
    )


def _emit_failure(component: str, exc: BaseException) -> None:
    """Best-effort telemetry; never re-raises."""
    logger = current_telemetry()
    if logger is None:
        return
    try:
        logger.log(
            "memory.hot_layer.failure",
            {"component": component, "error": str(exc)[:200]},
        )
    except Exception:  # pragma: no cover - belt-and-suspenders
        pass


def _read_canonical_blocks(
    workspace: Path,
) -> list[str]:
    """Render top N entity pages as ``=== CANONICAL ===`` blocks.

    Pages under ``archive/`` are skipped (absorbed records, surfaced only via
    ``durin memory expand``).

    Per-page parse failures degrade silently with a telemetry event;
    the rest of the walk continues so one bad page can't break the
    whole layer.
    """
    pages: list[tuple[str, str, EntityPage]] = []  # (sort_key, ref, page)
    for page_path in walk_class(workspace, "entities"):
        try:
            page = EntityPage.from_file(page_path)
        except Exception as exc:
            _emit_failure(f"canonical_blocks:{page_path.name}", exc)
            continue
        if page is None:
            continue
        slug = page_path.stem
        ref = f"{page.type}:{slug}"
        # Sort key: prefer updated_at, fall back to mtime so freshly
        # written pages surface even pre-frontmatter updated_at adoption.
        updated = _resolve_updated_at(page, page_path)
        pages.append((updated, ref, page))

    pages.sort(key=lambda t: t[0], reverse=True)
    blocks: list[str] = []
    total_chars = 0
    for sort_key, ref, page in pages[:_MAX_CANONICAL]:
        block = _render_canonical_block(ref, page, consolidated_ts=sort_key)
        if total_chars + len(block) > _CANONICAL_BUDGET_CHARS:
            break
        blocks.append(block)
        total_chars += len(block)
    return blocks


def _resolve_updated_at(page: EntityPage, page_path: Path) -> str:
    """Best-effort consolidation timestamp for the canonical marker.

    Order of preference: ``updated_at`` (v2/v1 frontmatter) → file
    mtime → sentinel. Returned as an ISO-8601 string for stable sort.
    """
    if page.updated_at is not None:
        return page.updated_at.isoformat()
    raw = page.extra.get("updated_at", "") if page.extra else ""
    if isinstance(raw, str) and raw:
        return raw
    try:
        return datetime.fromtimestamp(page_path.stat().st_mtime).isoformat()
    except OSError:
        return "0000-00-00"


def _render_canonical_block(
    ref: str,
    page: EntityPage,
    *,
    consolidated_ts: str,
) -> str:
    """Format one canonical entity page for the hot layer.

    Layout (per doc 06 §8.3 + Phase 1.5 v2 rendering):

        === CANONICAL: <ref> (consolidated <ts>) ===
        <name>[ (aliases: a, b, c)].
        Attributes: k1 is v1; k2 is v2.            # only if non-empty
        Relations: <type> of <to>[ (since N)]; ... # only if non-empty
        <body excerpt>
        === END CANONICAL ===

    v1 pages (no attributes/relations) render with just name + body;
    aliases line is omitted when ``aliases`` is empty. This keeps the
    block tight — no empty lines or label-only rows.
    """
    # G7 (audit fourth pass, 2026-05-28): single source of truth for
    # marker construction. `hot_layer` always supplies the
    # consolidated_ts so it gets the timestamped variant; the helper
    # also handles the no-ts case used by `sectioned_output`.
    from durin.memory.section_markers import canonical_marker
    header = canonical_marker(ref, ts=consolidated_ts)
    lines = [header, _render_name_line(page)]

    attr_line = _render_attributes_line(page.attributes)
    if attr_line:
        lines.append(attr_line)

    rel_line = _render_relations_line(page.relations)
    if rel_line:
        lines.append(rel_line)

    # Legacy ``identifiers`` (v1 emergent field) still rendered so
    # workspaces that haven't migrated to v2 attributes don't lose
    # identifier visibility in the hot layer.
    identifiers = page.extra.get("identifiers") if page.extra else None
    ident_line = _render_identifiers_line(identifiers)
    if ident_line:
        lines.append(ident_line)

    body = (page.body or "").strip()
    if body:
        lines.append(body[:_CANONICAL_BODY_PER_PAGE])
    from durin.memory.section_markers import end_marker
    lines.append(end_marker("canonical"))
    return "\n".join(lines)


def _render_name_line(page: EntityPage) -> str:
    """``<name>`` or ``<name> (aliases: a, b).`` — empty aliases stay silent."""
    if page.aliases:
        aliases = ", ".join(page.aliases[:5])
        return f"{page.name} (aliases: {aliases})."
    return page.name


def _render_attributes_line(attributes: dict[str, Any]) -> str:
    """Prose form: ``Attributes: k1 is v1; k2 is v2.`` Empty → ``""``."""
    if not attributes:
        return ""
    parts: list[str] = []
    for key, value in attributes.items():
        rendered_value = _render_attribute_value(value)
        if rendered_value is None:
            continue
        parts.append(f"{key} is {rendered_value}")
    if not parts:
        return ""
    return "Attributes: " + "; ".join(parts) + "."


def _render_attribute_value(value: Any) -> str | None:
    """Coerce one attribute value to prose. Returns None to skip the entry."""
    if value is None:
        return None
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        items = [_render_attribute_value(v) for v in value]
        items = [i for i in items if i]
        return ", ".join(items) if items else None
    if isinstance(value, dict):
        # Stateful attribute (doc 01 §4.3) — render compactly.
        sub_parts = []
        for k, v in value.items():
            rv = _render_attribute_value(v)
            if rv:
                sub_parts.append(f"{k}={rv}")
        return "{" + ", ".join(sub_parts) + "}" if sub_parts else None
    return str(value)


def _render_relations_line(relations: list[dict[str, Any]]) -> str:
    """Prose form: ``Relations: <type> of <to> (since N); ...``.

    Renders ``since`` when present (the most common temporal qualifier
    Dream emits per doc 01 §3.5). Other free-form metadata
    (``intensity``, ``role``, etc.) is dropped from the prose to keep
    the line tight — full data is still on disk and surfaces via the
    canonical drill / memory_search.
    """
    if not relations:
        return ""
    parts: list[str] = []
    for rel in relations:
        if not isinstance(rel, dict):
            continue
        target = rel.get("to")
        rtype = rel.get("type")
        if not isinstance(target, str) or not isinstance(rtype, str):
            continue
        chunk = f"{rtype} of {target}" if target.split(":", 1)[0] == "person" else f"{rtype} {target}"
        since = rel.get("since")
        if since not in (None, "", []):
            chunk += f" (since {since})"
        parts.append(chunk)
    if not parts:
        return ""
    return "Relations: " + "; ".join(parts) + "."


def _render_identifiers_line(identifiers: Any) -> str:
    """Legacy v1 emergent ``identifiers`` field → one prose line.

    Compatible with both shapes the dream LLM emits in practice: flat list or typed dict.
    Returns ``""`` when there's nothing meaningful to render.
    """
    if not identifiers:
        return ""
    if isinstance(identifiers, dict):
        parts: list[str] = []
        for kind, values in identifiers.items():
            if isinstance(values, list) and values:
                parts.append(f"{kind}: {', '.join(str(v) for v in values[:3])}")
            elif isinstance(values, str) and values:
                parts.append(f"{kind}: {values}")
        if parts:
            return "Identifiers — " + "; ".join(parts)
    if isinstance(identifiers, list) and identifiers:
        flat = [str(v) for v in identifiers[:5] if v]
        if flat:
            return "Identifiers — " + ", ".join(flat)
    return ""


def _read_fragment_blocks(
    workspace: Path,
) -> list[str]:
    """Render up to N recent entries from episodic/stable as FRAGMENT blocks.

    A fragment qualifies when its class is ``episodic`` or ``stable`` (corpus
    and pending out) and it tags at least one entity. Two-track model
    (2026-06-06, N3): fragments are NOT consolidated into entity pages, so there
    is no cursor-based "graduation" — recent tagged fragments surface by recency,
    capped by the budget.

    Untagged entries (``entry.entities == []``) are not "fragments
    amending a canonical" by definition — they surface via the
    headlines section instead.
    """
    candidates: list[tuple[str, str, Path]] = []  # (sort_key, rel_path, path)
    for class_name in _FRAGMENT_CLASSES:
        for path in walk_class(workspace, class_name):
            if path.name == "IDENTITY.md":
                # IDENTITY surfaces only via the identity section.
                continue
            try:
                entry = load_entry(path)
            except Exception:
                continue
            if not entry.entities:
                continue
            ts = entry.valid_from.isoformat() if entry.valid_from else ""
            try:
                rel = path.relative_to(workspace).as_posix()
            except ValueError:
                rel = path.name
            candidates.append((ts or "0000", rel, path))

    candidates.sort(key=lambda t: t[0], reverse=True)
    blocks: list[str] = []
    total_chars = 0
    for _, rel_path, path in candidates[:_MAX_FRAGMENTS]:
        try:
            entry = load_entry(path)
        except Exception:
            continue
        block = _render_fragment_block(entry, rel_path)
        if total_chars + len(block) > _FRAGMENTS_BUDGET_CHARS:
            break
        blocks.append(block)
        total_chars += len(block)
    return blocks


def _render_fragment_block(entry: Any, rel_path: str) -> str:
    """``=== FRAGMENT: <path> (ts <ts>) ===`` per doc 06 §8.3.

    G7 (audit fourth pass, 2026-05-28): delegates to the shared
    `section_markers` helper. ``unknown`` is preserved as the ts
    fallback when ``valid_from`` is missing — historical convention
    used by the hot layer; the helper accepts any ts string including
    ``unknown``.
    """
    from durin.memory.section_markers import end_marker, fragment_marker
    ts = entry.valid_from.isoformat() if entry.valid_from else "unknown"
    header = fragment_marker(rel_path, ts=ts)
    body = (entry.body or entry.summary or entry.headline or "").strip()
    body = body[:_FRAGMENT_BODY_PER_PAGE]
    return "\n".join([header, body, end_marker("fragment")])


def _is_at_or_before(entry_ts: str, cursor: Any) -> bool:
    """Mirror of entity_ranker._is_pre_cursor: parse datetimes safely."""
    if not entry_ts or cursor is None:
        return False
    if isinstance(cursor, (int, float)):
        return False
    try:
        et = datetime.fromisoformat(str(entry_ts).replace("Z", "+00:00"))
        ct = datetime.fromisoformat(str(cursor).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return et <= ct


def _read_identity(workspace: Path) -> str:
    """Return the identity body, trimmed to budget. Empty if missing."""
    path = workspace / "memory" / "stable" / "IDENTITY.md"
    if not path.is_file():
        return ""
    try:
        entry = load_entry(path)
        text = entry.body or entry.summary or entry.headline
    except Exception:
        # Allow non-frontmatter plain markdown as identity too.
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
    return text[:_IDENTITY_BUDGET_CHARS]


def _read_top_headlines(workspace: Path) -> list[str]:
    """Glob memory/<class>/*.md, sort by valid_from desc, trim to budget."""
    candidates: list[tuple[str, str]] = []  # (sort_key, headline)
    for class_name in MEMORY_CLASSES:
        for path in walk_class(workspace, class_name):
            if path.name == "IDENTITY.md":
                # Surfaced in the identity section already.
                continue
            try:
                entry = load_entry(path)
            except Exception:
                continue
            sort_key = entry.valid_from.isoformat() if entry.valid_from else "0000-00-00"
            candidates.append((sort_key, entry.headline))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return _trim_to_budget(
        [h for _, h in candidates[:_MAX_HEADLINES]],
        _HEADLINES_BUDGET_CHARS,
    )


def _read_entity_list(workspace: Path) -> list[str]:
    """Aggregate entities across all memory entries; dedup + alphabetise."""
    entities: set[str] = set()
    for class_name in MEMORY_CLASSES:
        for path in walk_class(workspace, class_name):
            try:
                entry = load_entry(path)
            except Exception:
                continue
            entities.update(entry.entities)

    return _trim_to_budget(sorted(entities)[:_MAX_ENTITIES], _ENTITIES_BUDGET_CHARS)


def _trim_to_budget(items: list[str], budget_chars: int) -> list[str]:
    """Drop items from the tail until the total character count fits."""
    total = 0
    out: list[str] = []
    for item in items:
        # +2 covers the "- " bullet prefix / ", " separator in either render mode.
        if total + len(item) + 2 > budget_chars:
            break
        out.append(item)
        total += len(item) + 2
    return out
