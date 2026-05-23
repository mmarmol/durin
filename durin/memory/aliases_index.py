"""Alias index — **in-memory only**, rebuilt from disk on demand.

Maps every identifying string (name + aliases + emergent identifiers)
to the **list of entity references** that own it. Per doc 18 §7 +
§10 R6: lookup returns a list because common aliases are ambiguous by
nature (`marcelo` could refer to >1 person). Disambiguation happens at
the consumer (write-time tagger and read-time ranker), not by hiding
the collision.

Shape::

    {
      "marcelo": ["person:marcelo_marmol", "person:marcelo_diaz"],
      "marcelo marmol": ["person:marcelo_marmol"],
      "mmarmol@mxhero.com": ["person:marcelo_marmol"],
      "durin": ["project:durin"]
    }

Keys are lowercase-folded so lookup is case-insensitive (per doc 18
§7 paso 6). Values are full entity refs (``<type>:<slug>``), where
*slug* is the filename — the authoritative identifier.

**No disk persistence** (doc 23 T1.4 + glm A3): for typical durin
corpus (cientos de entidades), `build()` is sub-second. Persisting a
JSON sidecar to disk introduces drift risk if a `.md` is edited
outside the tool (vim, git merge), so callers always rebuild on boot.
Mutations during runtime (via :meth:`add`, :meth:`remove`,
:meth:`refresh_for`) update the in-memory map only.

Archive subfolders (``<slug>/archive/``) are skipped — those entries
are intentionally de-indexed per doc 18 §3 + §10 R6.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from durin.memory.entity_page import EntityPage

__all__ = ["AliasIndex"]

logger = logging.getLogger(__name__)

_ARCHIVE_MARKER = "/archive/"


class AliasIndex:
    """In-memory alias map.

    Lifecycle:
    - ``build()`` — walk ``memory/entities/``, parse every page, populate.
      Always called at boot to rebuild from disk (no persistent sidecar).
    - ``add(page, slug)`` / ``remove(entity_ref)`` — incremental updates
      in memory (callers do not persist).
    - ``lookup(query)`` — return ordered list of candidate entity refs.
    """

    def __init__(self, memory_root: Path) -> None:
        self.memory_root = Path(memory_root)
        self._map: dict[str, list[str]] = defaultdict(list)

    # ------------------------------------------------------------------
    # build (rebuild-only — no disk persistence)
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Walk entities/ tree, parse pages, populate the index from scratch.

        Skips archive subfolders. Pages that fail to parse are logged and
        skipped without aborting the build.
        """
        self._map.clear()
        entities_root = self.memory_root / "entities"
        if not entities_root.exists():
            return
        for md_file in entities_root.rglob("*.md"):
            if _ARCHIVE_MARKER in str(md_file):
                continue
            try:
                page = EntityPage.from_file(md_file)
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("aliases_index: skip unreadable %s: %s",
                               md_file, exc)
                continue
            if page is None:
                logger.warning("aliases_index: skip unparseable %s", md_file)
                continue
            slug = EntityPage.slug_from_path(md_file)
            entity_ref = f"{page.type}:{slug}"
            self._populate(page.identifying_strings(), entity_ref)

    # ------------------------------------------------------------------
    # lookup (case-insensitive, returns LIST per doc 18 §7 + R6)
    # ------------------------------------------------------------------

    def lookup(self, query: str) -> list[str]:
        """Return the list of entity refs matching *query*.

        Empty list when no match. Result is **ordered**: entries are
        appended as encountered during build / incremental updates,
        deduped. Consumers (write-time tagger, read-time ranker)
        disambiguate by context when ``len(result) > 1``.
        """
        if not isinstance(query, str):
            return []
        return list(self._map.get(query.lower().strip(), []))

    def keys(self) -> Iterable[str]:
        """Iterate alias keys (for debugging / dashboards)."""
        return self._map.keys()

    def all_entities(self) -> set[str]:
        """Set of all entity refs known to the index."""
        out: set[str] = set()
        for refs in self._map.values():
            out.update(refs)
        return out

    def size(self) -> int:
        return len(self._map)

    # ------------------------------------------------------------------
    # incremental updates (for use when a page is saved/changed)
    # ------------------------------------------------------------------

    def add(self, page: EntityPage, slug: str) -> None:
        """Add/refresh an entity's aliases. Existing entries for the same
        entity ref are not deduped against the new ones — caller should
        ``remove()`` first if doing a full refresh.
        """
        entity_ref = f"{page.type}:{slug}"
        self._populate(page.identifying_strings(), entity_ref)

    def remove(self, entity_ref: str) -> None:
        """Drop all alias entries pointing to *entity_ref*. O(N) in keys."""
        to_delete: list[str] = []
        for key, refs in self._map.items():
            if entity_ref in refs:
                refs[:] = [r for r in refs if r != entity_ref]
                if not refs:
                    to_delete.append(key)
        for key in to_delete:
            del self._map[key]

    def refresh_for(self, page: EntityPage, slug: str) -> None:
        """Atomic ``remove(entity_ref) + add(page)``. Convenient for saves."""
        entity_ref = f"{page.type}:{slug}"
        self.remove(entity_ref)
        self.add(page, slug)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _populate(self, identifying_strings: list[str], entity_ref: str) -> None:
        seen: set[str] = set()
        for raw in identifying_strings:
            if not isinstance(raw, str):
                continue
            key = raw.lower().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            existing = self._map[key]
            if entity_ref not in existing:
                existing.append(entity_ref)
