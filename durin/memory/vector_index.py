"""LanceDB-backed vector index over memory entry summaries.

Phase 2.2 of the memory subsystem. The index lives at
``<workspace>/memory/.index.lance`` and holds one record per memory
entry: ``(id, class_name, summary, headline, vector, valid_from, path)``.
Vectors are produced by an :class:`~durin.memory.embedding.EmbeddingProvider`
(``FastembedProvider`` in V1).

Two write paths:

- :meth:`VectorIndex.upsert` — incremental, called by ``memory_store``
  after a single entry is written. Deletes any prior row with the same
  ``id`` and inserts the new one.
- :meth:`VectorIndex.rebuild_from_workspace` — full rebuild by walking
  ``memory/<class>/*.md``. Used at install time or when the index is
  out of sync.

Read path: :meth:`VectorIndex.search` returns the top-K nearest
records (LanceDB's ``L2`` distance by default — we don't override
because the embedding models we ship are normalized).

LanceDB is an optional install (``pip install durin[memory]``). The
import is lazy so the rest of durin still works without it; callers
should guard with :func:`vector_index_available` or accept the
``RuntimeError`` thrown on first use.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from durin.memory.embedding import EmbeddingProvider
from durin.memory.paths import MEMORY_CLASSES
from durin.memory.schema import MemoryEntry
from durin.memory.storage import load_entry

logger = logging.getLogger(__name__)

__all__ = ["VectorIndex", "vector_index_available"]


_TABLE_NAME = "memory_entries"
_INDEX_DIR = ".index.lance"


def vector_index_available() -> bool:
    """Return whether the optional lancedb dependency is importable."""
    try:
        import lancedb  # noqa: F401
    except ImportError:
        return False
    return True


class VectorIndex:
    """LanceDB table wrapper for memory entries."""

    def __init__(self, workspace: Path, provider: EmbeddingProvider) -> None:
        self._workspace = workspace
        self._provider = provider
        self._uri = str(workspace / "memory" / _INDEX_DIR)

    # ------------------------------------------------------------------
    # write paths
    # ------------------------------------------------------------------

    def upsert(self, entry: MemoryEntry, class_name: str, path: Path) -> None:
        """Add or replace one memory entry in the index.

        Idempotent on ``entry.id``: a prior row with the same id is
        deleted before the new one is inserted.
        """
        db = self._connect()
        record = self._record_for(entry, class_name, path)
        names = db.list_tables().tables
        if _TABLE_NAME in names:
            table = db.open_table(_TABLE_NAME)
            table.delete(f"id = '{_escape(entry.id)}'")
            table.add([record])
        else:
            db.create_table(_TABLE_NAME, data=[record])

    def rebuild_from_workspace(self) -> int:
        """Re-embed every ``memory/<class>/*.md`` entry; returns count rebuilt.

        Atomic from the consumer's perspective: the existing table is
        dropped only after the new one is built successfully.
        """
        entries: list[tuple[MemoryEntry, str, Path]] = []
        memory_root = self._workspace / "memory"
        if not memory_root.is_dir():
            return 0
        for class_name in MEMORY_CLASSES:
            class_dir = memory_root / class_name
            if not class_dir.is_dir():
                continue
            for path in sorted(class_dir.glob("*.md")):
                try:
                    entry = load_entry(path)
                except Exception:  # noqa: BLE001
                    logger.warning("vector_index: skipping malformed %s", path)
                    continue
                entries.append((entry, class_name, path))
        if not entries:
            self._drop_if_exists()
            return 0

        texts = [self._embed_text(entry) for entry, _, _ in entries]
        vectors = self._provider.embed(texts)
        records = [
            self._record_with_vector(entry, class_name, path, vec)
            for (entry, class_name, path), vec in zip(entries, vectors)
        ]

        db = self._connect()
        self._drop_if_exists(db)
        db.create_table(_TABLE_NAME, data=records)
        return len(records)

    # ------------------------------------------------------------------
    # read path
    # ------------------------------------------------------------------

    def search(self, query: str, *, top_k: int = 10) -> list[dict[str, Any]]:
        """Return the top-K nearest records to ``query`` (warm-tier shape)."""
        if not query.strip() or top_k <= 0:
            return []
        db = self._connect()
        if _TABLE_NAME not in db.list_tables().tables:
            return []
        [vec] = self._provider.embed([query])
        table = db.open_table(_TABLE_NAME)
        rows = table.search(vec).limit(top_k).to_list()
        # Drop the raw vector from the payload — callers don't need it,
        # and 1024 floats per row is wasted bandwidth back to the agent.
        for row in rows:
            row.pop("vector", None)
        return rows

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _record_for(
        self,
        entry: MemoryEntry,
        class_name: str,
        path: Path,
    ) -> dict[str, Any]:
        [vec] = self._provider.embed([self._embed_text(entry)])
        return self._record_with_vector(entry, class_name, path, vec)

    def _record_with_vector(
        self,
        entry: MemoryEntry,
        class_name: str,
        path: Path,
        vector: list[float],
    ) -> dict[str, Any]:
        try:
            rel_path = path.relative_to(self._workspace)
        except ValueError:
            rel_path = path
        return {
            "id": entry.id,
            "class_name": class_name,
            "summary": entry.summary,
            "headline": entry.headline,
            "vector": vector,
            "valid_from": entry.valid_from.isoformat() if entry.valid_from else "",
            "path": str(rel_path),
        }

    @staticmethod
    def _embed_text(entry: MemoryEntry) -> str:
        """What we actually feed the embedder. Summary > headline > body."""
        if entry.summary.strip():
            return entry.summary
        if entry.headline.strip():
            return entry.headline
        return entry.body

    def _connect(self):
        try:
            import lancedb  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "lancedb is required for vector retrieval. "
                "Install the memory extra: pip install durin[memory]"
            ) from exc
        Path(self._uri).parent.mkdir(parents=True, exist_ok=True)
        return lancedb.connect(self._uri)

    def _drop_if_exists(self, db: Any | None = None) -> None:
        if db is None:
            try:
                db = self._connect()
            except RuntimeError:
                return
        if _TABLE_NAME in db.list_tables().tables:
            db.drop_table(_TABLE_NAME)


def _escape(value: str) -> str:
    """Escape single quotes for LanceDB SQL-style filter strings."""
    return value.replace("'", "''")
