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
from durin.memory.paths import MEMORY_CLASSES, walk_class
from durin.memory.schema import MemoryEntry
from durin.memory.storage import load_entry

logger = logging.getLogger(__name__)

__all__ = [
    "VectorIndex",
    "VectorIndexDimensionMismatch",
    "vector_index_available",
]


_TABLE_NAME = "memory_entries"
_INDEX_DIR = ".index.lance"


class VectorIndexDimensionMismatch(RuntimeError):
    """The on-disk index has a vector dimension that disagrees with the
    embedding provider currently configured. Caller must rebuild via
    :meth:`VectorIndex.rebuild_from_workspace` or pick a model that
    matches the existing dimension."""


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
        deleted before the new one is inserted. Raises
        :class:`VectorIndexDimensionMismatch` if the on-disk table
        carries a different vector dim than the current provider —
        caller should rebuild via :meth:`rebuild_from_workspace` or
        revert the model change.
        """
        db = self._connect()
        record = self._record_for(entry, class_name, path)
        names = db.list_tables().tables
        if _TABLE_NAME in names:
            table = db.open_table(_TABLE_NAME)
            self._guard_dim_match(table, len(record["vector"]))
            table.delete(f"id = '{_escape(entry.id)}'")
            table.add([record])
        else:
            db.create_table(_TABLE_NAME, data=[record])

    def upsert_entity_page(
        self,
        *,
        entity_ref: str,
        name: str,
        aliases: list[str],
        body: str,
        path: Path,
    ) -> None:
        """Index a consolidated entity page (``memory/entities/<type>/<slug>.md``).

        Per ``docs/18_entity_centric_plan.md`` §7 + Phase 0.1 finding:
        the embedded text composes ``name + aliases + body`` **without**
        the ``<type>:`` prefix (which Phase 0.1 measured at cosine 0.517
        against ``durin``, vs 0.755 for ``durin``/``durin-agent`` — the
        prefix introduces token noise).

        Stored as ``class_name="entity_page"`` so search consumers can
        distinguish from memory entries via the same field that already
        carries the memory class for ``memory/<class>/*.md`` entries.
        """
        text = self._compose_entity_page_text(name=name, aliases=aliases, body=body)
        [vec] = self._provider.embed([text])
        try:
            rel_path = path.relative_to(self._workspace)
        except ValueError:
            rel_path = path
        # Synthesize a record that shares the table schema with memory
        # entries — `id` carries the entity_ref (with colon), `summary`
        # carries the display name + aliases for at-a-glance results.
        summary = name + (f" ({', '.join(aliases)})" if aliases else "")
        record: dict[str, Any] = {
            "id": entity_ref,
            "class_name": "entity_page",
            "summary": summary,
            "headline": name,
            "vector": vec,
            "valid_from": "",
            # B1: keep schema consistent with memory entries; entity pages
            # don't reference other entities (they ARE the entity), so the
            # list stays empty. The ranker treats class_name=="entity_page"
            # separately from tag-overlap logic.
            "entities": [],
            "path": str(rel_path),
            # P2.5: full body for cold-tier reads without disk hits.
            "body": body or "",
        }
        db = self._connect()
        names = db.list_tables().tables
        if _TABLE_NAME in names:
            table = db.open_table(_TABLE_NAME)
            self._guard_dim_match(table, len(vec))
            table.delete(f"id = '{_escape(entity_ref)}'")
            table.add([record])
        else:
            db.create_table(_TABLE_NAME, data=[record])

    _PAGE_EMBED_BUDGET_CHARS = 1500

    @classmethod
    def _compose_entity_page_text(
        cls,
        *,
        name: str,
        aliases: list[str],
        body: str,
    ) -> str:
        """Compose embedding text for an entity page.

        Layout: ``name`` (most distilled), then ``aliases`` joined,
        then ``body`` (longest, most truncatable). NO ``type:`` prefix.
        """
        budget = cls._PAGE_EMBED_BUDGET_CHARS
        parts: list[str] = []
        used = 0
        joiner_len = len("\n\n")

        def _add(piece: str) -> None:
            nonlocal used
            piece = piece.strip()
            if not piece:
                return
            remaining = budget - used
            if remaining <= 0:
                return
            extra = joiner_len if parts else 0
            allowed = remaining - extra
            if allowed <= 0:
                return
            chunk = piece[:allowed]
            parts.append(chunk)
            used += len(chunk) + extra

        _add(name)
        if aliases:
            _add("Aliases: " + ", ".join(a for a in aliases if a))
        _add(body)
        return "\n\n".join(parts) or name or "entity page"

    def embed_text(self, text: str) -> list[float]:
        """Compute the embedding vector for *text* using this index's provider.

        Convenience for callers (e.g. ``memory_store`` dedup check) that
        need to reuse the same embedding for both search and upsert
        (G5). Returns a single vector. ``text`` must be non-empty.
        """
        if not text:
            raise ValueError("embed_text: text must be non-empty")
        return self._provider.embed([text])[0]

    def search_by_vector(
        self,
        vector: list[float],
        *,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """Same as :meth:`search` but skips the embedding step.

        Used by callers (e.g. ``memory_store`` dedup check per doc 23
        T1.7 + G5) that have already computed the query embedding and
        want to reuse it for both dedup search AND upsert.
        """
        if top_k <= 0:
            return []
        db = self._connect()
        if _TABLE_NAME not in db.list_tables().tables:
            return []
        table = db.open_table(_TABLE_NAME)
        self._guard_dim_match(table, len(vector))
        rows = table.search(vector).limit(top_k).to_list()
        for row in rows:
            row.pop("vector", None)
        return rows

    def upsert_with_vector(
        self,
        entry: MemoryEntry,
        class_name: str,
        path: Path,
        *,
        precomputed_vector: list[float],
    ) -> None:
        """Variant of :meth:`upsert` that reuses a precomputed embedding.

        Per doc 23 T1.7 + G5: the write path is
        ``compute_embedding → search (dedup) → upsert``. Without this
        method, ``upsert`` would recompute the embedding internally,
        doubling the embed cost per write. Pass the same vector that
        was used for the dedup check.
        """
        record = self._record_with_vector(entry, class_name, path, precomputed_vector)
        db = self._connect()
        names = db.list_tables().tables
        if _TABLE_NAME in names:
            table = db.open_table(_TABLE_NAME)
            self._guard_dim_match(table, len(precomputed_vector))
            table.delete(f"id = '{_escape(entry.id)}'")
            table.add([record])
        else:
            db.create_table(_TABLE_NAME, data=[record])

    def delete_by_id(self, record_id: str) -> bool:
        """Drop a single row by ``id``. Returns True if the table existed.

        Used by Phase 5 absorption — when a page is archived, the
        ``entity_page`` row keyed to its entity_ref must come out of the
        searchable index. No-op when the table is absent.
        """
        db = self._connect()
        if _TABLE_NAME not in db.list_tables().tables:
            return False
        table = db.open_table(_TABLE_NAME)
        table.delete(f"id = '{_escape(record_id)}'")
        return True

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
            for path in walk_class(self._workspace, class_name):
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
        """Return the top-K nearest records to ``query`` (warm-tier shape).

        Raises :class:`VectorIndexDimensionMismatch` if the on-disk
        table's vector dim doesn't match the current provider — the
        ``memory_search`` tool catches this and falls back to grep.
        """
        if not query.strip() or top_k <= 0:
            return []
        db = self._connect()
        if _TABLE_NAME not in db.list_tables().tables:
            return []
        [vec] = self._provider.embed([query])
        table = db.open_table(_TABLE_NAME)
        self._guard_dim_match(table, len(vec))
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
            # B1 (doc 24 §7): persist entity tags so entity_ranker's
            # post-cursor boost can match c.get("entities", []) against
            # query entities. Without this column, the ranker can never
            # boost tagged entries — W1 would be inoperative.
            "entities": list(entry.entities),
            "path": str(rel_path),
            # P2.5 (doc 10): store the full body so cold-tier search
            # can return it without a disk read. Doubles the index
            # size at the benefit of avoiding N file opens per query.
            "body": entry.body or "",
        }

    _EMBED_BUDGET_CHARS = 1500  # ~375 tokens; e5-small max_seq is 512.

    @staticmethod
    def _embed_text(entry: MemoryEntry, *, budget_chars: int | None = None) -> str:
        """Build the text fed to the embedder.

        Composes ``headline → summary → entities → body`` in that order
        until the char budget is filled. Most distilled signal first
        (headline / summary), then named entities, then the longest and
        most truncatable part (body). Previously only ``summary`` (or
        headline / body as fallback) was embedded, which gave poor recall
        for corpus entries where the body carries the information and
        summary is empty.
        """
        budget = budget_chars if budget_chars is not None else VectorIndex._EMBED_BUDGET_CHARS
        parts: list[str] = []
        used = 0
        joiner_len = len("\n\n")

        def _add(piece: str) -> None:
            nonlocal used
            piece = piece.strip()
            if not piece:
                return
            remaining = budget - used
            if remaining <= 0:
                return
            extra = joiner_len if parts else 0
            allowed = remaining - extra
            if allowed <= 0:
                return
            chunk = piece[:allowed]
            parts.append(chunk)
            used += len(chunk) + extra

        _add(entry.headline)
        _add(entry.summary)
        if entry.entities:
            _add("Entities: " + ", ".join(entry.entities))
        _add(entry.body)

        text = "\n\n".join(parts)
        return text or entry.headline or "memory entry"

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

    @staticmethod
    def _guard_dim_match(table: Any, expected_dim: int) -> None:
        """Raise if the table's vector column dim differs from ``expected_dim``.

        Detects model-swap drift: a user changes ``memory.embedding.model``
        from 384-dim to 1024-dim, the existing LanceDB table still has
        384-dim vectors, and the next upsert/search would mix
        incompatible vectors silently. Better to fail loudly with an
        actionable message pointing at ``rebuild_from_workspace`` /
        the ``/memory reindex`` CLI command.

        Also checks for the ``entities`` column (B1 per doc 24): tables
        created before the ranker-aware schema lack this field. Without
        it, entity_ranker can never boost tagged entries → W1 inoperative.
        """
        try:
            schema = table.schema
            field = schema.field("vector")
            actual_dim = field.type.list_size
        except Exception:  # noqa: BLE001
            # Unknown schema shape — let downstream LanceDB raise.
            return
        if actual_dim != expected_dim:
            raise VectorIndexDimensionMismatch(
                f"On-disk vector index uses {actual_dim}-dim vectors but the "
                f"current embedding model produces {expected_dim}-dim. The "
                f"model was probably changed in config. Run `/memory reindex` "
                f"(or `durin memory reindex` from the CLI) to rebuild the "
                f"index, or revert memory.embedding.model to the previous "
                f"value to keep the existing index."
            )
        # B1: detect tables missing the entities column.
        try:
            schema.field("entities")
        except Exception:  # noqa: BLE001
            raise VectorIndexDimensionMismatch(
                "On-disk vector index is missing the 'entities' column "
                "(table schema predates the ranker integration). Run "
                "`durin memory reindex` to rebuild from markdown sources."
            )


def _escape(value: str) -> str:
    """Escape single quotes for LanceDB SQL-style filter strings."""
    return value.replace("'", "''")
