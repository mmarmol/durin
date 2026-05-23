"""Hot layer — the always-loaded memory section of the stable prompt tier.

Phase 1.9 of the memory subsystem. The hot layer is what the agent
carries in every prompt without any tool call: identity essentials,
canonical entity pages (the "main memory"), recent post-cursor
fragments (entries that have not yet been consolidated into a page),
top headlines and a de-duplicated entity name list. By design it
changes at most once per day (refreshed by dream); between dreams it
is read-only so the upstream provider's prompt cache stays warm across
an entire day.

V1 (pre-dream-auto-trigger) builds the hot layer directly from disk on
each prompt build. Ranking is naive: identity from
``memory/stable/IDENTITY.md`` if present, canonical pages sorted by
``updated_at`` desc, recent fragments are post-cursor entries sorted by
``valid_from`` desc.

Per doc 25 §2.H, canonical pages and recent fragments are wrapped in
``=== CANONICAL: <ref> ===`` / ``=== FRAGMENT: <ref> (ts: ...) ===``
markers so the LLM can reconcile read-time (doc 18 §6 promise:
"coexisten en los resultados de retrieval; el LLM reconcilia en read-
time con timestamps y contexto"). Same convention as the compaction
``=== ARCHIVED SUMMARY ===`` block (bitácora 2026-05-19).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from durin.memory.entity_page import EntityPage
from durin.memory.paths import MEMORY_CLASSES
from durin.memory.storage import load_entry

__all__ = ["HotLayer", "read_hot_layer"]

# Soft budgets matching docs/archive/08_memory_phase2_proposal.md
# §0c.7 (~1000 tokens) plus §2.H additions for canonical pages +
# recent fragments. Total: ~1900 tokens (still cache-friendly between
# dreams, well within stable tier budget).
_IDENTITY_BUDGET_CHARS = 800   # ~200 tokens
_CANONICAL_BUDGET_CHARS = 2400  # ~600 tokens — N entity pages
_FRAGMENTS_BUDGET_CHARS = 1200  # ~300 tokens — recent post-cursor entries
_HEADLINES_BUDGET_CHARS = 1200  # ~300 tokens — legacy class entries
_ENTITIES_BUDGET_CHARS = 600   # ~150 tokens
_MAX_CANONICAL = 12
_MAX_FRAGMENTS = 8
_MAX_HEADLINES = 12
_MAX_ENTITIES = 50


class HotLayer(NamedTuple):
    identity: str
    canonical_blocks: list[str]
    fragment_blocks: list[str]
    headlines: list[str]
    entities: list[str]

    def render(self) -> str:
        """Render the hot layer as markdown for the stable prompt tier.

        Section order follows the §2.H contract: identity → canonical
        (main memory) → fragments (recent, post-cursor) → headlines
        (legacy entries) → entity list. The LLM sees the canonical
        first, marked as authoritative; fragments come next with their
        timestamp so the model can reconcile temporal contradictions.
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
                "## Memory: Recent fragments (post-cursor)\n\n"
                "Episodic entries not yet consolidated into a canonical "
                "page. Reconcile with the canonical above using the "
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
    """Assemble the hot layer for a workspace."""
    canonicals, canonical_cursors = _read_canonical_blocks(workspace)
    return HotLayer(
        identity=_read_identity(workspace),
        canonical_blocks=canonicals,
        fragment_blocks=_read_fragment_blocks(workspace, canonical_cursors),
        headlines=_read_top_headlines(workspace),
        entities=_read_entity_list(workspace),
    )


def _read_canonical_blocks(
    workspace: Path,
) -> tuple[list[str], dict[str, Any]]:
    """Render top N entity pages as ``=== CANONICAL ===`` blocks.

    Returns ``(blocks, cursors)`` where ``cursors`` maps each entity
    ref to its ``dream_processed_through`` value — used by
    :func:`_read_fragment_blocks` to filter to post-cursor entries.
    Pages under ``archive/`` are skipped (absorbed records, surfaced
    only via ``durin memory expand``).
    """
    entities_root = workspace / "memory" / "entities"
    if not entities_root.is_dir():
        return [], {}

    pages: list[tuple[str, str, EntityPage]] = []  # (sort_key, ref, page)
    cursors: dict[str, Any] = {}
    for type_dir in sorted(entities_root.iterdir()):
        if not type_dir.is_dir():
            continue
        for page_path in sorted(type_dir.glob("*.md")):
            page = EntityPage.from_file(page_path)
            if page is None:
                continue
            slug = page_path.stem
            ref = f"{page.type}:{slug}"
            # Sort key: prefer updated_at, fall back to mtime so
            # freshly written pages always surface even pre-frontmatter
            # updated_at adoption.
            updated = page.extra.get("updated_at", "") if page.extra else ""
            if not isinstance(updated, str) or not updated:
                try:
                    updated = datetime.fromtimestamp(
                        page_path.stat().st_mtime
                    ).isoformat()
                except OSError:
                    updated = "0000-00-00"
            pages.append((updated, ref, page))
            if page.dream_processed_through is not None:
                cursors[ref] = page.dream_processed_through

    pages.sort(key=lambda t: t[0], reverse=True)
    blocks: list[str] = []
    total_chars = 0
    for sort_key, ref, page in pages[:_MAX_CANONICAL]:
        block = _render_canonical_block(ref, page)
        if total_chars + len(block) > _CANONICAL_BUDGET_CHARS:
            break
        blocks.append(block)
        total_chars += len(block)
    return blocks, cursors


def _render_canonical_block(ref: str, page: EntityPage) -> str:
    """Format one canonical entity page for the hot layer.

    Keeps the block compact: name + aliases on the header line,
    identifiers on a second line (if any), then a body excerpt capped
    at ~600 chars per page so 10 pages fit the canonical budget.
    """
    header = f"=== CANONICAL: {ref}"
    if page.aliases:
        header += f" (aliases: {', '.join(page.aliases[:5])})"
    header += " ==="

    lines = [header, page.name]
    identifiers = page.extra.get("identifiers") if page.extra else None
    if isinstance(identifiers, dict) and identifiers:
        flat = []
        for kind, values in identifiers.items():
            if isinstance(values, list) and values:
                flat.append(f"{kind}: {', '.join(str(v) for v in values[:3])}")
            elif isinstance(values, str) and values:
                flat.append(f"{kind}: {values}")
        if flat:
            lines.append("Identifiers — " + "; ".join(flat))
    body = (page.body or "").strip()
    if body:
        # Cap per-page body so a single huge page can't consume the
        # whole canonical budget.
        body = body[:600]
        lines.append(body)
    lines.append(f"=== END CANONICAL ===")
    return "\n".join(lines)


def _read_fragment_blocks(
    workspace: Path,
    cursors: dict[str, Any],
) -> list[str]:
    """Render up to N post-cursor episodic entries as FRAGMENT blocks.

    Entries are post-cursor when their ``valid_from`` is later than the
    ``dream_processed_through`` of every entity they tag (so all
    canonical pages for those entities still need this fragment).
    Entries that pre-date every relevant cursor are already absorbed
    and skipped — surfacing them again would defeat the point of the
    canonical page.
    """
    episodic_dir = workspace / "memory" / "episodic"
    if not episodic_dir.is_dir():
        return []

    candidates: list[tuple[str, Path]] = []  # (sort_key desc, path)
    for path in episodic_dir.glob("*.md"):
        try:
            entry = load_entry(path)
        except Exception:
            continue
        # §2.H semantics: a fragment "amends a canonical" — entries with
        # no entity tag are not fragments in this sense. They surface
        # via the legacy headlines section (or not at all).
        if not entry.entities:
            continue
        ts = entry.valid_from.isoformat() if entry.valid_from else ""
        if ts and any(ref in cursors for ref in entry.entities):
            if all(
                _is_at_or_before(ts, cursors.get(ref))
                for ref in entry.entities
                if ref in cursors
            ):
                # Every relevant cursor already covers this entry —
                # canonical already absorbed it, skip.
                continue
        candidates.append((ts or "0000", path))

    candidates.sort(key=lambda t: t[0], reverse=True)
    blocks: list[str] = []
    total_chars = 0
    for _, path in candidates[:_MAX_FRAGMENTS]:
        try:
            entry = load_entry(path)
        except Exception:
            continue
        block = _render_fragment_block(entry, path)
        if total_chars + len(block) > _FRAGMENTS_BUDGET_CHARS:
            break
        blocks.append(block)
        total_chars += len(block)
    return blocks


def _render_fragment_block(entry: Any, path: Path) -> str:
    ref_hint = ", ".join(entry.entities) if entry.entities else path.stem
    ts = entry.valid_from.isoformat() if entry.valid_from else "unknown"
    header = f"=== FRAGMENT: {ref_hint} (ts: {ts}) ==="
    body = (entry.body or entry.summary or entry.headline or "").strip()[:400]
    return "\n".join([header, body, f"=== END FRAGMENT ==="])


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
    memory_root = workspace / "memory"
    if not memory_root.is_dir():
        return []

    candidates: list[tuple[str, str]] = []  # (sort_key, headline)
    for class_name in MEMORY_CLASSES:
        class_dir = memory_root / class_name
        if not class_dir.is_dir():
            continue
        for path in class_dir.glob("*.md"):
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
    memory_root = workspace / "memory"
    if not memory_root.is_dir():
        return []

    entities: set[str] = set()
    for class_name in MEMORY_CLASSES:
        class_dir = memory_root / class_name
        if not class_dir.is_dir():
            continue
        for path in class_dir.glob("*.md"):
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
