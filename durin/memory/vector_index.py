"""LanceDB-backed vector index over memory entry summaries.

Phase 2.2 of the memory subsystem. The index lives at
``<workspace>/.durin/index/lance/`` (moved 2026-05-30 — P9 vault-
friendly cleanup; was previously ``<workspace>/memory/.index.lance``
where it polluted the markdown-vault folder layout). Holds one record
per memory entry: ``(id, class_name, summary, headline, vector,
valid_from, path)``. Vectors are produced by an
:class:`~durin.memory.embedding.EmbeddingProvider` (``FastembedProvider``
in V1).

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
# Relative path from workspace root. Moved 2026-05-30 (P9) from
# `memory/.index.lance` to `.durin/index/lance/` so the `memory/`
# folder stays pure markdown (vault-friendly: Obsidian and the
# webui MemoryGraphView no longer see the binary blob mixed with
# the .md files they render).
_INDEX_PATH = (".durin", "index", "lance")


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


SUMMARY_FALLBACK_CHARS: int = 400
"""H4 (audit 2026-05-29): module-level export of the body-prefix cap so
the FTS indexer (`durin/memory/indexer.py`) shares the same value as
the vector index. See ``VectorIndex._SUMMARY_FALLBACK_CHARS`` for the
class-level alias kept for backward compat."""


def _effective_summary(entry: MemoryEntry) -> str:
    """Return the summary to materialise into the index row.

    Authoritative when the source carries one; otherwise the
    body-prefix fallback. Empty source body + empty source summary
    yields an empty string — there's nothing to fall back on.
    """
    persisted = (entry.summary or "").strip()
    if persisted:
        return entry.summary
    body = entry.body or ""
    if not body:
        return ""
    return body[:SUMMARY_FALLBACK_CHARS]


def _is_body_prefix(summary: str, body: str) -> bool:
    """True when ``summary`` is exactly the leading slice of ``body``.

    The H4 dedup marker: if the summary is just a body prefix (the
    fallback case), embedding it as its own slot would re-weight the
    same tokens twice. Authoritative summaries (Dream / user-supplied)
    rarely match a body prefix verbatim, so the comparison is safe.
    """
    if not summary or not body:
        return False
    return body.startswith(summary)


class VectorIndex:
    """LanceDB table wrapper for memory entries."""

    def __init__(self, workspace: Path, provider: EmbeddingProvider) -> None:
        self._workspace = workspace
        self._provider = provider
        # P9 (2026-05-30): index lives at `.durin/index/lance/`, no
        # longer inside `memory/`. Keeps the vault clean of binary
        # files for Obsidian / MemoryGraphView consumers.
        self._uri = str(workspace.joinpath(*_INDEX_PATH))

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
        attributes: dict[str, Any] | None = None,
        relations: list[dict[str, Any]] | None = None,
    ) -> None:
        """Index a consolidated entity page (``memory/entities/<type>/<slug>.md``).

        Per ``docs/archive/35_entity_centric_plan.md`` §7 + Phase 0.1 finding,
        the embedded text omits the ``<type>:`` prefix (which Phase 0.1
        measured at cosine 0.517 against ``durin``, vs 0.755 for
        ``durin``/``durin-agent`` — the prefix introduces token noise).

        Audit E9 (2026-05-28) ships v2.a: when `attributes` and
        `relations` are passed, they're rendered into the embedding
        text between aliases and body so attribute queries
        ("Marcelo's email", "who is X's spouse") hit the centroide.
        Pre-E9 callsites that pass only `name`/`aliases`/`body`
        continue to work — frontmatter defaults to empty.

        Stored as ``class_name="entity_page"`` so search consumers can
        distinguish from memory entries via the same field that already
        carries the memory class for ``memory/<class>/*.md`` entries.
        """
        text = self._compose_entity_page_text(
            name=name, aliases=aliases, body=body,
            attributes=attributes, relations=relations,
        )
        # Use embed_passages (not raw embed) so E5-family models get the
        # required `passage: ` prefix. For non-E5 models this is a no-op.
        [vec] = self._provider.embed_passages([text])
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
            # H5 (audit 2026-05-29): schema parity with memory-entry
            # rows. Entity pages don't carry a single ``body`` field
            # in the same shape as MemoryEntry; the closest analogue
            # is the rendered page body the caller passed. Persisting
            # its length lets the renderer compute completeness for
            # entity hits with the same logic as fragments.
            "body_length": len(body or ""),
            "vector": vec,
            "valid_from": "",
            # B1: keep schema consistent with memory entries; entity pages
            # don't reference other entities (they ARE the entity), so the
            # list stays empty. The ranker treats class_name=="entity_page"
            # separately from tag-overlap logic.
            "entities": [],
            "path": str(rel_path),
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

    # E9 (audit second pass, 2026-05-28): attribute keys that are
    # internal metadata, never rendered into the embedding centroid.
    _SKIP_FRONTMATTER_KEYS = frozenset({
        "created_at", "updated_at", "provenance",
        "dream_processed_through",
    })

    @classmethod
    def _render_frontmatter(
        cls,
        *,
        attributes: dict[str, Any] | None,
        relations: list[dict[str, Any]] | None,
    ) -> str:
        """E9 v2.a: render structured attributes + relations as prose
        for the embedding centroid.

        Rules (doc 02 §4.2 v2):
        - Stateful attributes (`{current, history}`) render only
          `current` to avoid centroid drift toward defunct facts.
        - Internal metadata (provenance, timestamps) is skipped.
        - Relations render as `Type.title(): <to_name or uri> (since
          <date>)`. We don't resolve names against the alias index
          here (the index isn't available at compose-time without
          extra plumbing); future-work: resolve via shared alias
          cache if recall on relation queries is below target.
        - Empty inputs return an empty string so the caller can skip
          the section cleanly.
        """
        sentences: list[str] = []

        if attributes:
            for key, value in attributes.items():
                if key in cls._SKIP_FRONTMATTER_KEYS:
                    continue
                if isinstance(value, dict) and "current" in value:
                    rendered = value.get("current")
                    if rendered is None or rendered == "":
                        continue
                    sentences.append(
                        f"{cls._title_key(key)}: {rendered}."
                    )
                elif value is None or value == "":
                    continue
                elif isinstance(value, (list, tuple)):
                    if not value:
                        continue
                    sentences.append(
                        f"{cls._title_key(key)}: {', '.join(str(v) for v in value)}."
                    )
                else:
                    sentences.append(
                        f"{cls._title_key(key)}: {value}."
                    )

        if relations:
            for rel in relations:
                rel_type = rel.get("type")
                to = rel.get("to")
                since = rel.get("since")
                if not rel_type or not to:
                    continue
                target = str(to).split(":", 1)[-1] or str(to)
                clause = f"{cls._title_key(rel_type)}: {target}"
                if since:
                    clause = f"{clause} (since {since})"
                sentences.append(clause + ".")

        return " ".join(sentences)

    @staticmethod
    def _title_key(key: str) -> str:
        """`current_residence` -> `Current Residence`."""
        return " ".join(part.capitalize() for part in str(key).split("_"))

    @classmethod
    def compose_embedding_text(
        cls,
        item: Any,
        *,
        attributes: dict[str, Any] | None = None,
        relations: list[dict[str, Any]] | None = None,
        budget_chars: int | None = None,
    ) -> str:
        """Single public API for composing the embedding text.

        Audit F12 (2026-05-28): doc 02 §4 promised this method as the
        single source of truth for embedding composition. Pre-F12 the
        two specialised composers (`_compose_entity_page_text` for
        EntityPage objects and `_embed_text` for MemoryEntry rows)
        were the only entry points, leaving the documented public
        name unimplemented. The function exists now as a dispatcher
        — it routes to the correct specialist based on the input
        type so callers can stop guessing.
        """
        from durin.memory.entity_page import EntityPage as _EntityPage
        from durin.memory.storage import MemoryEntry as _MemoryEntry

        if isinstance(item, _EntityPage):
            return cls._compose_entity_page_text(
                name=item.name,
                aliases=list(item.aliases or []),
                body=item.body,
                attributes=item.attributes if attributes is None else attributes,
                relations=item.relations if relations is None else relations,
            )
        if isinstance(item, _MemoryEntry):
            return cls._embed_text(item, budget_chars=budget_chars)
        raise TypeError(
            f"compose_embedding_text: unsupported item type {type(item).__name__}; "
            "expected EntityPage or MemoryEntry"
        )

    @classmethod
    def _compose_entity_page_text(
        cls,
        *,
        name: str,
        aliases: list[str],
        body: str,
        attributes: dict[str, Any] | None = None,
        relations: list[dict[str, Any]] | None = None,
    ) -> str:
        """Compose embedding text for an entity page.

        Layout (v2.a, audit E9 2026-05-28):
        ``name`` → ``aliases`` → ``rendered_frontmatter`` → ``body``,
        in order, until 1500-char budget exhausted. Most distilled
        signal first (name); structured attributes/relations next so
        attribute queries hit the centroide; body (longest, most
        truncatable) last.

        Pre-E9 layout was ``name + aliases + body`` (v1) — the
        ``attributes`` and ``relations`` params default to None so
        existing callsites continue to work; v1 behaviour is the
        empty-frontmatter case (see test_compose_empty_attributes).
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
        rendered_fm = cls._render_frontmatter(
            attributes=attributes, relations=relations,
        )
        if rendered_fm:
            _add(rendered_fm)
        _add(body)
        return "\n\n".join(parts) or name or "entity page"

    def embed_text(self, text: str) -> list[float]:
        """Compute the embedding vector for *text* using this index's provider.

        Convenience for callers (e.g. ``memory_store`` dedup check) that
        need to reuse the same embedding for both search and upsert
        (G5). Returns a single vector. ``text`` must be non-empty.

        Uses passage-style embedding (E5 prefix when applicable) since
        the primary caller (memory_store dedup) embeds content that is
        about to be stored — passage-vs-passage similarity is the right
        comparison for "is this content already in the index?".
        """
        if not text:
            raise ValueError("embed_text: text must be non-empty")
        return self._provider.embed_passages([text])[0]

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
        """Re-embed every ``memory/<class>/*.md`` entry AND every
        ``memory/entities/<type>/<slug>.md`` page; returns total count
        rebuilt.

        Atomic from the consumer's perspective: the existing table is
        dropped only after the new one is built successfully.

        Audit E9 (2026-05-28) extended this to also walk entity pages.
        Pre-E9, only memory entries (`memory/<class>/*.md`) were walked
        — entity pages were re-upserted only by Dream apply or absorb,
        which meant that after a forced rebuild (e.g. schema version
        bump) entity pages were silently missing from the vector index
        until the next consolidation pass. Now the rebuild is complete.
        """
        memory_root = self._workspace / "memory"
        if not memory_root.is_dir():
            return 0

        # Pass 1: walk classified memory entries.
        entries: list[tuple[MemoryEntry, str, Path]] = []
        for class_name in MEMORY_CLASSES:
            for path in walk_class(self._workspace, class_name):
                try:
                    entry = load_entry(path)
                except Exception:  # noqa: BLE001
                    logger.warning("vector_index: skipping malformed %s", path)
                    continue
                entries.append((entry, class_name, path))

        # Pass 2: walk entity pages. Carry attributes/relations into the
        # embed text via v2.a composition (audit E9). `from_file`
        # returns None for malformed pages (frontmatter missing or
        # invalid) — skip them silently like the rest of the walker.
        from durin.memory.entity_page import EntityPage
        entity_pages: list[tuple[EntityPage, Path]] = []
        entities_root = memory_root / "entities"
        if entities_root.is_dir():
            for md_file in sorted(entities_root.rglob("*.md")):
                try:
                    page = EntityPage.from_file(md_file)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "vector_index: skipping malformed entity page %s",
                        md_file,
                    )
                    continue
                if page is None:
                    continue
                entity_pages.append((page, md_file))

        if not entries and not entity_pages:
            self._drop_if_exists()
            return 0

        # Embed batches: entries first, then entity pages.
        entry_texts = [self._embed_text(entry) for entry, _, _ in entries]
        page_texts = [
            self._compose_entity_page_text(
                name=page.name,
                aliases=list(page.aliases),
                body=page.body,
                attributes=dict(page.attributes),
                relations=list(page.relations),
            )
            for page, _ in entity_pages
        ]
        # Batch write — both entries and entity pages are passages, so
        # use embed_passages to apply E5 prefix uniformly.
        all_vectors = self._provider.embed_passages(entry_texts + page_texts) if (
            entry_texts or page_texts
        ) else []
        entry_vectors = all_vectors[: len(entry_texts)]
        page_vectors = all_vectors[len(entry_texts):]

        records = [
            self._record_with_vector(entry, class_name, path, vec)
            for (entry, class_name, path), vec
            in zip(entries, entry_vectors)
        ]
        records.extend(
            self._entity_page_record(page, md_file, vec)
            for (page, md_file), vec
            in zip(entity_pages, page_vectors)
        )

        db = self._connect()
        self._drop_if_exists(db)
        db.create_table(_TABLE_NAME, data=records)
        return len(records)

    def _entity_page_record(
        self,
        page: Any,  # durin.memory.entity_page.EntityPage
        md_file: Path,
        vec: list[float],
    ) -> dict[str, Any]:
        """Build the LanceDB row for an entity page, sharing the
        table schema with memory entries (E9 rebuild support)."""
        try:
            rel_path = md_file.relative_to(self._workspace)
        except ValueError:
            rel_path = md_file
        entity_ref = f"{page.type}:{md_file.stem}"
        summary = page.name + (
            f" ({', '.join(page.aliases)})" if page.aliases else ""
        )
        return {
            "id": entity_ref,
            "class_name": "entity_page",
            "summary": summary,
            "headline": page.name,
            "vector": vec,
            "valid_from": "",
            "entities": [],
            "path": str(rel_path),
        }

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
        # Use embed_query so E5-family models get the `query: ` prefix.
        vec = self._provider.embed_query(query)
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
        # Single-entry write — passage context.
        [vec] = self._provider.embed_passages([self._embed_text(entry)])
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
            # H4 (audit 2026-05-29): when the source has no summary
            # (bench seeds, memory_ingest chunks, raw episodic turns
            # all leave it empty by design — Dream is the intended
            # summary author but doesn't process corpus and only
            # consolidates episodic into entity_pages), materialise
            # ``body[:SUMMARY_FALLBACK_CHARS]`` into the index row.
            # The .md on disk keeps ``summary: ''`` as a legitimate
            # pre-Dream state; the index always carries triage content
            # so the renderer never hands the LLM a 60-char truncated
            # headline as the only signal. When Dream / memory_store
            # later populates the source's real summary, the next
            # upsert overwrites the fallback with the authoritative
            # value.
            "summary": _effective_summary(entry),
            "headline": entry.headline,
            # H5 (audit 2026-05-29): persist the source body's total
            # length so the search pipeline / renderer can compute the
            # per-hit completeness qualifier (``complete`` vs
            # ``preview N/M``). Without this, every hit would have to
            # guess whether drilling reveals more — the agent drills
            # defensively. ``len()`` on a Python str is char count
            # (consistent with the H4 ``body[:400]`` slice unit).
            "body_length": len(entry.body or ""),
            "vector": vector,
            "valid_from": entry.valid_from.isoformat() if entry.valid_from else "",
            # B1 (doc 24 §7): persist entity tags so entity_ranker's
            # post-cursor boost can match c.get("entities", []) against
            # query entities. Without this column, the ranker can never
            # boost tagged entries — W1 would be inoperative.
            "entities": list(entry.entities),
            "path": str(rel_path),
            # NOTE: `body` is deliberately NOT stored here. The .md on
            # disk is the single source of truth; the search pipeline
            # reads the body on demand via `memory_search._enrich_body`
            # when level=cold is requested. P2.5 (commit a266344)
            # briefly stored body here for a latency micro-optimisation,
            # reverted in audit A4 because it duplicated content and
            # opened a drift window between disk edits and LanceDB
            # reads. See docs/memory/08_scope_and_discarded.md §2.10.
        }

    _EMBED_BUDGET_CHARS = 1500  # ~375 tokens; e5-small max_seq is 512.

    # H4: class-level alias of the module-level
    # ``SUMMARY_FALLBACK_CHARS`` — kept so existing callers that read
    # via ``VectorIndex._SUMMARY_FALLBACK_CHARS`` keep working.
    _SUMMARY_FALLBACK_CHARS = SUMMARY_FALLBACK_CHARS

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
        # H4 (audit 2026-05-29): when ``summary`` is the body-prefix
        # fallback (carried for renderer / triage use, not as new
        # semantic signal), embedding it AS WELL AS body would weight
        # those tokens twice and shrink the budget available to unique
        # body content. Skip the summary slot when it matches the
        # leading slice of the body. Authoritative summaries (Dream
        # output, memory_store explicit) survive intact because they
        # describe the entry differently from its body prefix.
        if not _is_body_prefix(entry.summary, entry.body):
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
