"""Alias index — **in-memory only**, rebuilt from disk on demand.

Maps every identifying string (name + aliases + emergent identifiers)
to the **list of entity references** that own it. Lookup returns a list
because common aliases are ambiguous by nature (`marcelo` could refer to
>1 person). Disambiguation happens at
the consumer (write-time tagger and read-time ranker), not by hiding
the collision.

Shape::

    {
      "marcelo": ["person:marcelo_marmol", "person:marcelo_diaz"],
      "marcelo marmol": ["person:marcelo_marmol"],
      "mmarmol@mxhero.com": ["person:marcelo_marmol"],
      "durin": ["project:durin"]
    }

Keys are lowercase-folded so lookup is case-insensitive. Values are full entity refs (``<type>:<slug>``), where
*slug* is the filename — the authoritative identifier.

**No disk persistence**: for typical durin
corpus, `build()` is sub-second. Persisting a
JSON sidecar to disk introduces drift risk if a `.md` is edited
outside the tool (vim, git merge), so callers always rebuild on boot.
Mutations during runtime (via :meth:`add`, :meth:`remove`,
:meth:`refresh_for`) update the in-memory map only.

Archive subfolders (``<slug>/archive/``) are skipped — those entries
are intentionally de-indexed.
"""

from __future__ import annotations

import logging
import threading
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
        # A single instance is shared process-wide via
        # `aliases_cache.get_shared_alias_index`. Read paths (search /
        # recall threads calling `lookup`/`all_entities`) run concurrently
        # with the threshold-triggered Dream daemon thread mutating the
        # map via `refresh_for`/`remove`/`build`. Reentrant because
        # `refresh_for` nests `remove` + `add`, and `build` calls
        # `_populate`, each of which also takes the lock.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # build (rebuild-only — no disk persistence)
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Walk entities/ tree, parse pages, populate the index from scratch.

        Skips archive subfolders. Pages that fail to parse are logged and
        skipped without aborting the build.

        After the entities/ walk, also derive minimal aliases from the
        ``entities:`` frontmatter of `memory/<class>/*.md` entries.
        This means entity_ranker activates even in cold workspaces
        where Dream hasn't yet created canonical pages. Entity_page
        aliases take precedence (richer info), the episodic-derived
        aliases are append-only and only add the bare slug.
        """
        # Walk memory_root/entities/ directly. We skip any path that
        # contains an `archive/` component anywhere in its parts — that
        # covers both the spec's top-level `memory/archive/entities/...`
        # (which only matters if memory_root happens to point at the
        # workspace root) and the legacy nested `<slug>/archive/...`
        # layout that may still exist in older workspaces.
        from durin.memory.paths import MEMORY_CLASSES

        # Build into a fresh map and swap it in atomically under the lock
        # so concurrent readers never observe the post-clear / partially
        # repopulated state. The disk walk (sub-second but not free) runs
        # outside the lock; only the final swap is guarded.
        new_map: dict[str, list[str]] = defaultdict(list)

        entities_root = self.memory_root / "entities"
        if entities_root.is_dir():
            for md_file in sorted(entities_root.rglob("*.md")):
                rel = md_file.relative_to(entities_root)
                if "archive" in rel.parts:
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
                self._populate_into(new_map, page.identifying_strings(), entity_ref)

        # Derive minimal aliases from episodic / stable / corpus
        # / pending entries. We only index the *slug* portion of each
        # entity ref — splitting underscored slugs into sub-tokens
        # (data_migration → data, migration) is an anti-pattern that
        # pollutes search with noisy partial matches. The full slug
        # suffices as a discovery hook; richer aliases come from
        # entity_pages once Dream populates them.
        from durin.memory.storage import load_entry

        # Iterate every entry class (including pending — original
        # behavior preserved; walk_class only excludes pending when
        # called as `walk_memory` general scan).
        for class_name in MEMORY_CLASSES:
            class_dir = self.memory_root / class_name
            if not class_dir.is_dir():
                continue
            # Direct glob OK here: per-class entry dirs have only
            # top-level .md files (no nested archive in entry classes).
            for md_file in sorted(class_dir.glob("*.md")):
                try:
                    entry = load_entry(md_file)
                except Exception:  # noqa: BLE001
                    # Corrupted entries skip silently — same policy as
                    # entity_pages above.
                    continue
                for entity_ref in entry.entities:
                    if not isinstance(entity_ref, str) or ":" not in entity_ref:
                        continue
                    _, slug = entity_ref.split(":", 1)
                    if not slug:
                        continue
                    # _populate dedups: if entity_page already mapped
                    # this slug to this ref, it's a no-op.
                    self._populate_into(new_map, [slug], entity_ref)

        with self._lock:
            self._map = new_map

    # ------------------------------------------------------------------
    # lookup (case-insensitive, returns LIST)
    # ------------------------------------------------------------------

    def lookup(self, query: str) -> list[str]:
        """Return the list of entity refs matching *query*.

        Empty list when no match. Result is **ordered**: entries are
        appended as encountered during build / incremental updates, deduped.
        Consumers (write-time tagger, read-time ranker) disambiguate by
        context when ``len(result) > 1``.
        """
        if not isinstance(query, str):
            return []
        with self._lock:
            return list(self._map.get(query.lower().strip(), []))

    def keys(self) -> Iterable[str]:
        """Iterate alias keys (for debugging / dashboards).

        Returns a snapshot ``list`` (not a live view) so the caller can
        iterate without holding the lock while a writer mutates the map.
        """
        with self._lock:
            return list(self._map.keys())

    def all_entities(self) -> set[str]:
        """Set of all entity refs known to the index."""
        out: set[str] = set()
        with self._lock:
            for refs in self._map.values():
                out.update(refs)
        return out

    def size(self) -> int:
        with self._lock:
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
        with self._lock:
            self._populate_into(self._map, page.identifying_strings(), entity_ref)

    def remove(self, entity_ref: str) -> None:
        """Drop all alias entries pointing to *entity_ref*. O(N) in keys."""
        with self._lock:
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
        # Reentrant lock makes the remove+add pair atomic w.r.t. readers:
        # a concurrent `lookup` sees either the old or the new state,
        # never the empty gap between remove and add.
        with self._lock:
            self.remove(entity_ref)
            self.add(page, slug)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _populate_into(
        self,
        target: dict[str, list[str]],
        identifying_strings: list[str],
        entity_ref: str,
    ) -> None:
        """Add ``entity_ref`` under every identifying string into *target*.

        Lock-free: callers (``build`` on a thread-local map, ``add`` while
        holding ``self._lock``) own the synchronization. Operating on an
        explicit target lets ``build`` populate a fresh map off-lock and
        swap it in atomically.
        """
        seen: set[str] = set()
        for raw in identifying_strings:
            if not isinstance(raw, str):
                continue
            key = raw.lower().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            existing = target[key]
            if entity_ref not in existing:
                existing.append(entity_ref)
