"""Hot layer — the always-loaded memory section of the stable prompt tier.

Phase 1.9 of the memory subsystem. The hot layer is what the agent
carries in every prompt without any tool call: a small block of
identity essentials, top headlines from recent memory entries, and a
de-duplicated entity name list. By design it changes at most once per
day (refreshed by dream in Phase 3); between dreams it is read-only so
the upstream provider's prompt cache stays warm across an entire day.

V1 (no dream yet) builds the hot layer directly from disk on each
prompt build. Ranking is naive: identity from
``memory/stable/IDENTITY.md`` if present, headlines sorted by
``valid_from`` descending (newer wins), entities aggregated and
alphabetised. Phase 3 will replace the ranking with multi-factor
scoring and cache the result in ``memory/stable/_hot.cache``.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from durin.memory.paths import MEMORY_CLASSES
from durin.memory.storage import load_entry

__all__ = ["HotLayer", "read_hot_layer"]

# Soft budgets matching docs/08 §0c.7 (~1000 tokens total, ~4 chars per token).
_IDENTITY_BUDGET_CHARS = 800   # ~200 tokens
_HEADLINES_BUDGET_CHARS = 2000  # ~500 tokens
_ENTITIES_BUDGET_CHARS = 800   # ~200 tokens
_MAX_HEADLINES = 20
_MAX_ENTITIES = 50


class HotLayer(NamedTuple):
    identity: str
    headlines: list[str]
    entities: list[str]

    def render(self) -> str:
        """Render the hot layer as markdown for the stable prompt tier."""
        parts: list[str] = []
        if self.identity.strip():
            parts.append(f"## Memory: Identity\n\n{self.identity}")
        if self.headlines:
            bullets = "\n".join(f"- {h}" for h in self.headlines)
            parts.append(f"## Memory: Key Points\n\n{bullets}")
        if self.entities:
            csv = ", ".join(self.entities)
            parts.append(f"## Memory: Known Entities\n\n{csv}")
        return "\n\n".join(parts)


def read_hot_layer(workspace: Path) -> HotLayer:
    """Assemble the hot layer for a workspace."""
    return HotLayer(
        identity=_read_identity(workspace),
        headlines=_read_top_headlines(workspace),
        entities=_read_entity_list(workspace),
    )


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
