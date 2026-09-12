"""LanceDB-backed vector index over memory entry summaries.

The index lives at ``<workspace>/.durin/index/lance/``. Holds three
record types — memory entry, entity page, and skill — sharing the
same row schema: ``(id, class_name, summary, headline, vector,
valid_from, entities, path, body_length)``. Vectors are produced by an
:class:`~durin.memory.embedding.EmbeddingProvider` (``FastembedProvider``
in V1).

Two write paths:

- :meth:`VectorIndex.upsert` — incremental, called by
  ``indexer.reindex_one_file_vector`` after a single entry's file is
  written (file watcher, health-check repair). Deletes any prior row
  with the same ``id`` and inserts the new one.
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
from typing import Any, Sequence

from durin.memory.embedding import EmbeddingProvider
from durin.memory.paths import MEMORY_CLASSES, skill_uri, walk_class
from durin.memory.schema import MemoryEntry
from durin.memory.storage import load_entry
from durin.utils.file_lock import cross_process_lock

logger = logging.getLogger(__name__)

__all__ = [
    "VectorIndex",
    "VectorIndexDimensionMismatchError",
    "prune_orphan_rows",
    "vector_id_for_uri",
    "vector_index_available",
]


_TABLE_NAME = "memory_entries"
# Relative path from workspace root, inside the `.durin/` hidden
# directory. Keeps the `memory/` folder pure markdown (vault-friendly:
# Obsidian and the webui MemoryGraphView no longer see the binary blob
# mixed with the .md files they render).
_INDEX_PATH = (".durin", "index", "lance")


class VectorIndexDimensionMismatchError(RuntimeError):
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
"""Module-level export of the body-prefix cap so the FTS indexer
(`durin/memory/indexer.py`) shares the same value as the vector index.
See ``VectorIndex._SUMMARY_FALLBACK_CHARS`` for the class-level alias
kept for backward compat."""


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


def _table_schema(dims: int) -> Any:
    """Explicit pyarrow schema for ``_TABLE_NAME``, in the field order
    every write path already produces: ``id``, ``class_name``,
    ``summary``, ``headline``, ``path``, ``valid_from`` (all ``string``),
    ``body_length`` (``int64``), ``vector`` (``fixed_size_list<float32>``
    of ``dims``), ``entities`` (``list<string>``).

    Passed to every ``create_table`` call so a fresh table's columns are
    declared up front instead of inferred from the first record. Without
    it, a table whose first row happens to carry ``entities: []`` (an
    entity page, a skill, or a reference chunk — none of which tag
    entities) is created with ``entities`` typed ``list<null>``, and no
    later record with populated entities can ever be upserted into it
    (:meth:`VectorIndex.migrate_schema` repairs a table already in that
    state).

    lancedb/pyarrow are optional installs, so the import stays inside
    the function like every other lancedb access in this module.
    """
    import pyarrow as pa

    return pa.schema([
        pa.field("id", pa.string()),
        pa.field("class_name", pa.string()),
        pa.field("summary", pa.string()),
        pa.field("headline", pa.string()),
        pa.field("path", pa.string()),
        pa.field("valid_from", pa.string()),
        pa.field("body_length", pa.int64()),
        pa.field("vector", pa.list_(pa.float32(), dims)),
        pa.field("entities", pa.list_(pa.string())),
    ])


class VectorIndex:
    """LanceDB table wrapper for memory entries."""

    def __init__(self, workspace: Path, provider: EmbeddingProvider) -> None:
        self._workspace = workspace
        self._provider = provider
        # Index lives at `.durin/index/lance/`, no longer inside
        # `memory/`. Keeps the vault clean of binary files for
        # Obsidian / MemoryGraphView consumers.
        self._uri = str(workspace.joinpath(*_INDEX_PATH))

    # ------------------------------------------------------------------
    # write paths
    # ------------------------------------------------------------------

    @staticmethod
    def _atomic_upsert(table: Any, record: dict[str, Any]) -> None:
        """Replace-or-insert ``record`` by ``id`` in a single commit (B6).

        Supersedes a ``delete`` + ``add`` pair, which committed twice and
        left a window where a concurrent reader — or a failure between the
        two commits — saw the row missing. ``merge_insert`` applies as one
        atomic LanceDB commit.
        """
        (
            table.merge_insert("id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute([record])
        )

    def upsert(self, entry: MemoryEntry, class_name: str, path: Path) -> None:
        """Add or replace one memory entry in the index.

        Idempotent on ``entry.id``: a prior row with the same id is
        deleted before the new one is inserted. Raises
        :class:`VectorIndexDimensionMismatchError` if the on-disk table
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
            self._atomic_upsert(table, record)
        else:
            db.create_table(
                _TABLE_NAME, data=[record], schema=_table_schema(len(record["vector"])),
            )

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

        The embedded text omits the ``<type>:`` prefix (which was measured
        at cosine 0.517 against ``durin``, vs 0.755 for
        ``durin``/``durin-agent`` — the prefix introduces token noise).

        Schema version 2.a: when `attributes` and `relations` are
        passed, they're rendered into the embedding text between
        aliases and body so attribute queries ("Marcelo's email",
        "who is X's spouse") hit the centroide. Callsites that pass
        only `name`/`aliases`/`body` continue to work — frontmatter
        defaults to empty.

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
            # Schema parity with memory-entry rows. Entity pages don't
            # carry a single ``body`` field in the same shape as
            # MemoryEntry; the closest analogue is the rendered page
            # body the caller passed. Persisting its length lets the
            # renderer compute completeness for entity hits with the
            # same logic as fragments.
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
            self._atomic_upsert(table, record)
        else:
            db.create_table(
                _TABLE_NAME, data=[record], schema=_table_schema(len(vec)),
            )

    def upsert_skill(
        self,
        *,
        name: str,
        description: str,
        body: str,
        path: Any,
        mode: str = "",
    ) -> None:
        """Index a skill page (``skills/<slug>/SKILL.md``) into the vector store.

        Mirrors :meth:`upsert_entity_page`: composes the embedding text,
        embeds it via :meth:`EmbeddingProvider.embed_passages` (so
        E5-family models get the ``passage: `` prefix), synthesises a
        record sharing the table schema, then atomically replaces-or-
        inserts by ``id`` (or creates the table on first write).

        Stored as ``class_name="skill"`` and keyed by
        :func:`~durin.memory.paths.skill_uri` (``skill/<slug>``) so search
        consumers can distinguish skills from memory entries and entity
        pages via the same field. ``mode`` is accepted for callsite
        symmetry with ``SkillPage`` but does not enter the embedding.
        """
        text = f"{name}\n{description}\n{body}"
        # Use embed_passages (not raw embed) so E5-family models get the
        # required `passage: ` prefix. For non-E5 models this is a no-op.
        [vec] = self._provider.embed_passages([text])
        record: dict[str, Any] = {
            "id": skill_uri(name),
            "class_name": "skill",
            "summary": description,
            "headline": name,
            "body_length": len(body or ""),
            "vector": vec,
            "valid_from": "",
            "entities": [],
            "path": str(path),
        }
        db = self._connect()
        names = db.list_tables().tables
        if _TABLE_NAME in names:
            table = db.open_table(_TABLE_NAME)
            self._guard_dim_match(table, len(vec))
            self._atomic_upsert(table, record)
        else:
            db.create_table(
                _TABLE_NAME, data=[record], schema=_table_schema(len(vec)),
            )

    def upsert_reference_chunk(
        self,
        *,
        ref: str,
        idx: int,
        text: str,
        path: Any,
        breadcrumb: str = "",
    ) -> None:
        """Index one structure-aware chunk of a reference document.

        The whole reference doc is the FTS unit (``indexer._payload_for``); the
        chunks are the vector unit. The row ``id`` is ``<ref>#<idx>`` so a
        vector hit on a fragment resolves to its parent reference (strip
        ``#<idx>``); ``class_name`` is ``"reference"`` to match the FTS/grep
        reader side.

        ``breadcrumb`` is the chunk's heading path (``Chapter › Section``); it
        is prepended to the embedded passage so an isolated chunk carries its
        section context, and it prefixes the display ``summary``. It is not a
        separate column — the raw text lives in the ``.chunks.jsonl`` sidecar.
        """
        passage = f"{breadcrumb}\n\n{text}" if breadcrumb else text
        [vec] = self._provider.embed_passages([passage])
        try:
            rel_path = Path(path).relative_to(self._workspace)
        except ValueError:
            rel_path = Path(path)
        summary = f"[{breadcrumb}] {text}" if breadcrumb else text
        record: dict[str, Any] = {
            "id": f"{ref}#{idx}",
            "class_name": "reference",
            "summary": summary[:200],
            "headline": ref,
            "body_length": len(text or ""),
            "vector": vec,
            "valid_from": "",
            "entities": [],
            "path": str(rel_path),
        }
        db = self._connect()
        names = db.list_tables().tables
        if _TABLE_NAME in names:
            table = db.open_table(_TABLE_NAME)
            self._guard_dim_match(table, len(vec))
            self._atomic_upsert(table, record)
        else:
            db.create_table(
                _TABLE_NAME, data=[record], schema=_table_schema(len(vec)),
            )

    def _skill_record(
        self,
        skill: Any,  # durin.memory.skill_page.SkillPage
        md_file: Path,
        vec: list[float],
    ) -> dict[str, Any]:
        """Build the LanceDB row for a skill page, sharing the table
        schema with memory entries and entity pages (rebuild Pass 3)."""
        try:
            rel_path = md_file.relative_to(self._workspace)
        except ValueError:
            rel_path = md_file
        return {
            "id": skill_uri(skill.name),
            "class_name": "skill",
            "summary": skill.description,
            "headline": skill.name,
            "body_length": len(skill.body or ""),
            "vector": vec,
            "valid_from": "",
            "entities": [],
            "path": str(rel_path),
        }

    _PAGE_EMBED_BUDGET_CHARS = 1500

    # Attribute keys that are internal metadata, never rendered into
    # the embedding centroid.
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

        Rules:
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

        Layout (v2.a):
        ``name`` → ``aliases`` → ``rendered_frontmatter`` → ``body``,
        in order, until 1500-char budget exhausted. Most distilled
        signal first (name); structured attributes/relations next so
        attribute queries hit the centroide; body (longest, most
        truncatable) last.

        The prior layout was ``name + aliases + body`` (v1) — the
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

    def search_by_vector(
        self,
        vector: list[float],
        *,
        top_k: int = 10,
        where: str | None = None,
    ) -> list[dict[str, Any]]:
        """Same as :meth:`search` but skips the embedding step.

        ``where`` is a LanceDB filter over the row columns (``class_name``,
        ``id``), applied before the top-k.

        Used by callers that have already computed the query embedding
        and want to reuse it rather than re-embed via :meth:`search`.
        """
        if top_k <= 0:
            return []
        db = self._connect()
        if _TABLE_NAME not in db.list_tables().tables:
            return []
        table = db.open_table(_TABLE_NAME)
        self._guard_dim_match(table, len(vector))
        query_builder = table.search(vector)
        if where:
            # A prefilter: the top-k is taken among rows the predicate keeps,
            # so a population that dominates the table cannot fill the window.
            query_builder = query_builder.where(where, prefilter=True)
        rows = query_builder.limit(top_k).to_list()
        for row in rows:
            row.pop("vector", None)
        return rows

    def ids_by_class(self, class_names: Sequence[str]) -> set[str]:
        """The ``id`` of every row whose ``class_name`` is in ``class_names``.

        Used by :func:`durin.memory.indexer.backfill_missing_vectors` to
        diff the FTS uris against what's already embedded. Filters
        ``class_name`` in the LanceDB query itself (not in Python) and
        projects only ``id`` without reading the vector column.

        ``count_rows()`` is read once, before the query runs, to size the
        ``limit``; a row inserted between the two calls can be missed.
        The next backfill run picks it up — embedding is idempotent, so
        a missed row costs one extra embed, not a correctness bug.
        """
        if not class_names:
            return set()
        db = self._connect()
        if _TABLE_NAME not in db.list_tables().tables:
            return set()
        table = db.open_table(_TABLE_NAME)
        total = table.count_rows()
        if total == 0:
            return set()
        quoted = ",".join(f"'{_escape(c)}'" for c in class_names)
        rows = (
            table.search()
            .where(f"class_name IN ({quoted})")
            .select(["id"])
            .limit(total)
            .to_list()
        )
        return {row["id"] for row in rows}

    def entity_page_vectors(self) -> dict[str, list[float]]:
        """The stored vector of every entity page keyed by its ``<type>:<slug>``
        id, in one scan of the table — no embedding.

        Backs the dream's semantic duplicate walk: the stored vector is the
        passage embedding of the page's composed text, so the walk compares
        passages with passages instead of re-embedding every page as a query.
        Sized like :meth:`ids_by_class` (``count_rows()`` once for the limit);
        a row inserted in between is picked up on the next run.
        """
        db = self._connect()
        if _TABLE_NAME not in db.list_tables().tables:
            return {}
        table = db.open_table(_TABLE_NAME)
        total = table.count_rows()
        if total == 0:
            return {}
        rows = (
            table.search()
            .where("class_name = 'entity_page'")
            .select(["id", "vector"])
            .limit(total)
            .to_list()
        )
        return {
            row["id"]: [float(x) for x in row["vector"]]
            for row in rows
            if isinstance(row.get("id"), str) and row.get("vector") is not None
        }

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Passage embeddings for ``texts`` through the index's own provider —
        the same embedding every stored row carries, so a caller mixing fresh
        vectors with :meth:`entity_page_vectors` compares like with like."""
        if not texts:
            return []
        return [list(vec) for vec in self._provider.embed_passages(texts)]

    def delete_by_id(self, record_id: str) -> bool:
        """Drop a single row by ``id``. Returns True if the table existed.

        Used by the absorption step — when a page is archived, the
        ``entity_page`` row keyed to its entity_ref must come out of the
        searchable index. No-op when the table is absent.
        """
        db = self._connect()
        if _TABLE_NAME not in db.list_tables().tables:
            return False
        table = db.open_table(_TABLE_NAME)
        table.delete(f"id = '{_escape(record_id)}'")
        return True

    def migrate_schema(self) -> bool:
        """Repair a table created before the explicit schema (F-A).

        A table created by the old ``create_table(data=[record])`` call
        (no schema) had its columns inferred from the first row. Every
        write path that could plausibly be first — entity pages, skills,
        reference chunks — persists ``entities: []``, so the column was
        inferred ``list<null>``: a shape LanceDB can never cast to
        ``list<string>`` in place (``alter_columns`` raises), so the
        first later entry carrying real entity tags fails to upsert.

        Rewrites the table under :func:`~durin.utils.file_lock.cross_process_lock`
        (the same lock :meth:`rebuild_from_workspace` uses, keyed off this
        index's uri): ``to_arrow()`` reads every row including its vector,
        the ``entities`` column is replaced with an all-empty
        ``list<string>`` column (a null list carries no data to lose), and
        the result is written back with ``create_table(..., mode="overwrite")``.

        Idempotent: a table whose ``entities`` column is already typed
        (or a table that doesn't exist) is left untouched and this
        returns False. Rows are re-checked once the lock is held, so two
        callers racing this method can't both perform the rewrite.
        """
        db = self._connect()
        if _TABLE_NAME not in db.list_tables().tables:
            return False
        import pyarrow as pa

        with cross_process_lock(Path(self._uri)):
            table = db.open_table(_TABLE_NAME)
            try:
                field = table.schema.field("entities")
            except Exception:  # noqa: BLE001
                return False
            value_type = getattr(field.type, "value_type", None)
            if value_type is None or not pa.types.is_null(value_type):
                return False
            arrow_table = table.to_arrow()
            row_count = arrow_table.num_rows
            column_index = arrow_table.schema.get_field_index("entities")
            empty_entities = pa.array(
                [[]] * row_count, type=pa.list_(pa.string()),
            )
            arrow_table = arrow_table.set_column(
                column_index, "entities", empty_entities,
            )
            db.create_table(_TABLE_NAME, data=arrow_table, mode="overwrite")
        logger.info(
            "vector_index: migrated %s's entities column from list<null> "
            "to list<string> (%d row(s) preserved)", _TABLE_NAME, row_count,
        )
        return True

    def rebuild_from_workspace(self) -> int:
        """Re-embed every ``memory/<class>/*.md`` entry, every
        ``memory/entities/<type>/<slug>.md`` page, AND every
        ``skills/<slug>/SKILL.md`` skill; returns total count rebuilt.

        NOT atomic: the existing table is dropped, then the new one is
        created (`_drop_if_exists` then `create_table`), so there is a
        brief window with no table. Concurrent readers tolerate it —
        `search` returns `[]` and the only caller (`_safe_vector_search`)
        degrades to grep; `upsert` callers are likewise guarded. This
        path runs only on a forced reindex or the health-check rebuild
        after the lance probe detects breakage, never on the normal
        write path, so the window is accepted rather than swapped out.

        Two processes' health-check rebuilds can race the drop+create
        window and corrupt or lose rows. The body is wrapped in
        ``cross_process_lock`` so only one rebuild runs at a time. The
        per-row ``merge_insert`` upsert paths are already atomic and are
        intentionally left outside this lock.

        Also walks entity pages: only memory entries
        (`memory/<class>/*.md`) were originally walked — entity pages
        were re-upserted only by Dream apply or absorb, which meant
        that after a forced rebuild (e.g. schema version bump) entity
        pages were silently missing from the vector index until the
        next consolidation pass. Skills (Pass 3) were added
        later: they live under `skills/`, an independent root, so the
        rebuild walks them even when `memory/` is absent.
        """
        with cross_process_lock(Path(self._uri)):
            return self._rebuild_body()

    def _rebuild_body(self) -> int:
        """Execute the rebuild under the caller-held cross-process lock."""
        # Pass 1: walk classified memory entries. `walk_class` guards on
        # the class dir's existence, so this is safe when `memory/` is
        # absent (e.g. a skills-only workspace).
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
        # embed text via v2.a composition. `from_file`
        # returns None for malformed pages (frontmatter missing or
        # invalid) — skip them silently like the rest of the walker.
        from durin.memory.entity_page import EntityPage
        entity_pages: list[tuple[EntityPage, Path]] = []
        entities_root = self._workspace / "memory" / "entities"
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

        # Pass 3: walk skills (`skills/<slug>/SKILL.md`). Disabled skills
        # (`disable_model_invocation`) stay out of the searchable index.
        # `walk_skills` guards on the `skills/` root's existence and
        # skips `_`-prefixed dirs (symmetry with `walk_memory`).
        # Gated by `memory.index_skills`: when off, skills are never
        # embedded into the vector table (clean no-op even with SKILL.md
        # files on disk).
        from durin.memory.index_meta import skills_indexing_enabled
        from durin.memory.paths import walk_skills
        from durin.memory.skill_page import SkillPage
        skills: list[tuple[SkillPage, Path]] = []
        skill_walk = walk_skills(self._workspace) if skills_indexing_enabled() else ()
        for md in skill_walk:
            try:
                sp = SkillPage.from_file(md)
            except Exception:  # noqa: BLE001
                logger.warning("vector_index: skipping malformed skill %s", md)
                continue
            if sp is None or sp.disabled:
                continue
            skills.append((sp, md))

        # Pass 4: references — the token-aware chunks are the vector unit.
        # A forced rebuild MUST restore them too, else a `durin memory reindex`
        # (or the N5 model-change rebuild) silently drops reference semantic
        # search — only ingest-time indexing would survive.
        from durin.memory.reference import reference_chunks
        # (ref, idx, text, md, breadcrumb)
        ref_chunks: list[tuple[str, int, str, Path, str]] = []
        refs_root = self._workspace / "memory" / "references"
        if refs_root.is_dir():
            for md_file in sorted(refs_root.glob("*.md")):
                ref = f"reference:{md_file.stem}"
                for rec in reference_chunks(self._workspace, ref):
                    ref_chunks.append((
                        ref, rec["idx"], rec["text"], md_file,
                        rec.get("breadcrumb", ""),
                    ))

        if not entries and not entity_pages and not skills and not ref_chunks:
            self._drop_if_exists()
            return 0

        # Embed batches: entries first, then entity pages, then skills.
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
        skill_texts = [
            f"{sp.name}\n{sp.description}\n{sp.body}" for sp, _ in skills
        ]
        # Batch write — entries, entity pages, and skills are all
        # passages, so use embed_passages to apply E5 prefix uniformly.
        ref_texts = [
            f"{bc}\n\n{text}" if bc else text
            for (_ref, _idx, text, _md, bc) in ref_chunks
        ]
        all_texts = entry_texts + page_texts + skill_texts + ref_texts
        all_vectors = self._provider.embed_passages(all_texts) if all_texts else []
        n_e, n_p, n_s = len(entry_texts), len(page_texts), len(skill_texts)
        entry_vectors = all_vectors[:n_e]
        page_vectors = all_vectors[n_e:n_e + n_p]
        skill_vectors = all_vectors[n_e + n_p:n_e + n_p + n_s]
        ref_vectors = all_vectors[n_e + n_p + n_s:]

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
        records.extend(
            self._skill_record(sp, md_file, vec)
            for (sp, md_file), vec
            in zip(skills, skill_vectors)
        )
        for (ref, idx, text, md_file, bc), vec in zip(ref_chunks, ref_vectors):
            try:
                rel_p = md_file.relative_to(self._workspace)
            except ValueError:
                rel_p = md_file
            summary = f"[{bc}] {text}" if bc else text
            records.append({
                "id": f"{ref}#{idx}",
                "class_name": "reference",
                "summary": summary[:200],
                "headline": ref,
                "body_length": len(text or ""),
                "vector": vec,
                "valid_from": "",
                "entities": [],
                "path": str(rel_p),
            })

        db = self._connect()
        self._drop_if_exists(db)
        # `records` is non-empty here (the all-empty case returned above),
        # so its first row's vector carries the real embedded dimension —
        # reading it directly from the data avoids trusting a provider's
        # `dimensions` property to agree with what `embed_passages` just
        # produced. Entity-page records don't carry `body_length` (B1);
        # `create_table(data=<list of dicts>, schema=...)` requires every
        # dict's key count to already match the schema's column count, so
        # the records are converted to a `pa.Table` against the schema
        # first — `pa.Table.from_pylist` fills a record's missing keys
        # with null, `create_table` does not.
        import pyarrow as pa

        schema = _table_schema(len(records[0]["vector"]))
        db.create_table(_TABLE_NAME, data=pa.Table.from_pylist(records, schema=schema))
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

    def search(
        self, query: str, *, top_k: int = 10, where: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the top-K nearest records to ``query`` (warm-tier shape).

        ``where`` is a LanceDB filter over the row columns (``class_name``,
        ``id``), applied before the top-k.

        Raises :class:`VectorIndexDimensionMismatchError` if the on-disk
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
        query_builder = table.search(vec)
        if where:
            # A prefilter: the top-k is taken among rows the predicate keeps,
            # so a population that dominates the table cannot fill the window.
            query_builder = query_builder.where(where, prefilter=True)
        rows = query_builder.limit(top_k).to_list()
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
            # When the source has no summary (bench seeds,
            # memory_ingest chunks, raw episodic turns all leave it
            # empty by design — Dream is the intended summary author
            # but doesn't process corpus and only consolidates episodic
            # into entity_pages), materialise
            # ``body[:SUMMARY_FALLBACK_CHARS]`` into the index row.
            # The .md on disk keeps ``summary: ''`` as a legitimate
            # pre-Dream state; the index always carries triage content
            # so the renderer never hands the LLM a 60-char truncated
            # headline as the only signal. When Dream / `/remember`
            # later populates the source's real summary, the next
            # upsert overwrites the fallback with the authoritative
            # value.
            "summary": _effective_summary(entry),
            "headline": entry.headline,
            # Persist the source body's total length so the search
            # pipeline / renderer can compute the per-hit completeness
            # qualifier (``complete`` vs ``preview N/M``). Without
            # this, every hit would have to guess whether drilling
            # reveals more — the agent drills defensively. ``len()``
            # on a Python str is char count (consistent with the
            # ``body[:400]`` slice unit).
            "body_length": len(entry.body or ""),
            "vector": vector,
            "valid_from": entry.valid_from.isoformat() if entry.valid_from else "",
            # Persist entity tags so entity_ranker's entity-match boost
            # can match c.get("entities", []) against query entities.
            # Without this column, the ranker can never boost tagged
            # entries.
            "entities": list(entry.entities),
            "path": str(rel_path),
            # NOTE: `body` is deliberately NOT stored here. The .md on
            # disk is the single source of truth; the search pipeline
            # reads the body on demand via `memory_search._enrich_body`
            # when level=cold is requested. Storing body here was briefly
            # attempted as a latency micro-optimisation but reverted
            # because it duplicated content and opened a drift window
            # between disk edits and LanceDB reads.
        }

    _EMBED_BUDGET_CHARS = 1500  # ~375 tokens; e5-small max_seq is 512.
    # Entities + Topics lines share this many chars of the embed budget.
    # An entry's tag lists can grow large (a session summary's union
    # accumulates for the life of its key) — without a bound here, a long
    # list would eat most or all of the remaining budget via `_add`'s own
    # truncation, leaving the body — the embedder's actual semantic
    # signal — with little or nothing. This caps the tags' worst-case
    # share regardless of how many the entry carries.
    _TAG_LINES_BUDGET_CHARS = 300

    # H4: class-level alias of the module-level
    # ``SUMMARY_FALLBACK_CHARS`` — kept so existing callers that read
    # via ``VectorIndex._SUMMARY_FALLBACK_CHARS`` keep working.
    _SUMMARY_FALLBACK_CHARS = SUMMARY_FALLBACK_CHARS

    @staticmethod
    def _tag_lines(entry: MemoryEntry, budget: int) -> list[str]:
        """Build the ``Entities:``/``Topics:`` lines, the pair sharing
        *budget* chars total.

        Tags are dropped whole from the end of each list to fit — never a
        raw mid-string slice — so every surviving tag stays well-formed.
        Entities are filled first (mirrors the field order elsewhere in
        this method); Topics get whatever budget remains.
        """
        lines: list[str] = []
        remaining = budget
        for prefix, tags in (("Entities", entry.entities), ("Topics", entry.topics)):
            if not tags or remaining <= 0:
                continue
            kept: list[str] = []
            used = len(prefix) + 2  # "<prefix>: "
            for tag in tags:
                added = len(tag) + (2 if kept else 0)  # ", " separator
                if used + added > remaining:
                    break
                kept.append(tag)
                used += added
            if kept:
                line = f"{prefix}: " + ", ".join(kept)
                lines.append(line)
                remaining -= len(line)
        return lines

    @staticmethod
    def _embed_text(entry: MemoryEntry, *, budget_chars: int | None = None) -> str:
        """Build the text fed to the embedder.

        Composes ``headline → summary → entities → topics → body`` in that
        order until the char budget is filled. Most distilled signal first
        (headline / summary), then the named entities and subject labels
        (query-shaped words the body often never spells out), then the
        longest and most truncatable part (body). Previously only
        ``summary`` (or headline / body as fallback) was embedded, which
        gave poor recall for corpus entries where the body carries the
        information and summary is empty.
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
        # When ``summary`` is the body-prefix fallback (carried for
        # renderer / triage use, not as new semantic signal), embedding
        # it AS WELL AS body would weight those tokens twice and shrink
        # the budget available to unique body content. Skip the summary
        # slot when it matches the leading slice of the body.
        # Authoritative summaries (Dream output, `/remember` explicit)
        # survive intact because they describe the entry differently
        # from its body prefix.
        if not _is_body_prefix(entry.summary, entry.body):
            _add(entry.summary)
        for line in VectorIndex._tag_lines(entry, VectorIndex._TAG_LINES_BUDGET_CHARS):
            _add(line)
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

        Also checks for the ``entities`` column: tables created before
        the ranker-aware schema lack this field. Without it,
        entity_ranker can never boost tagged entries.
        """
        try:
            schema = table.schema
            field = schema.field("vector")
            actual_dim = field.type.list_size
        except Exception:  # noqa: BLE001
            # Unknown schema shape — let downstream LanceDB raise.
            return
        if actual_dim != expected_dim:
            raise VectorIndexDimensionMismatchError(
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
            # Translate the cryptic lance schema-lookup error into an
            # actionable domain error; the message is self-explanatory so we
            # suppress the original cause (B904).
            raise VectorIndexDimensionMismatchError(
                "On-disk vector index is missing the 'entities' column "
                "(table schema predates the ranker integration). Run "
                "`durin memory reindex` to rebuild from markdown sources."
            ) from None


def _escape(value: str) -> str:
    """Escape single quotes for LanceDB SQL-style filter strings."""
    return value.replace("'", "''")


_DELETE_CHUNK = 500


def delete_ids(workspace: Path, ids: list[str]) -> int:
    """Drop rows by ``id`` WITHOUT an embedding provider — no model load.

    Shared by :func:`durin.memory.forget.forget_entry` and the health-check
    self-heal so neither pays the embedding-model load just to remove index
    rows. Batches into ``id IN (...)`` deletes (chunked) so reconciling a
    bulk out-of-band deletion stays a handful of ops, not one-per-row.

    No-op (returns 0) when lancedb is unavailable or the table is absent.
    Returns the number of distinct ids requested (LanceDB delete does not
    report affected rows).
    """
    unique = [i for i in dict.fromkeys(ids) if i]
    if not unique:
        return 0
    try:
        import lancedb  # type: ignore[import-not-found]
    except ImportError:
        return 0
    uri = str(Path(workspace).joinpath(*_INDEX_PATH))
    if not Path(uri).is_dir():
        return 0
    db = lancedb.connect(uri)
    if _TABLE_NAME not in db.list_tables().tables:
        return 0
    table = db.open_table(_TABLE_NAME)
    for start in range(0, len(unique), _DELETE_CHUNK):
        chunk = unique[start:start + _DELETE_CHUNK]
        quoted = ",".join(f"'{_escape(i)}'" for i in chunk)
        table.delete(f"id IN ({quoted})")
    return len(unique)


def vector_id_for_uri(uri: str) -> str:
    """Map an index ``uri`` to its vector-table ``id``.

    Memory entries are stored under the bare entry id
    (``memory/<class>/<id>`` → ``<id>``); entity refs (``type:slug``) and
    skills (``skill/<slug>``) use the uri verbatim. Single source of truth
    shared by the health-check reconcile (``HealthChecker._vector_id_for``)
    and the watcher delete path (``indexer.reindex_one_file``) so the two
    can't drift.
    """
    if uri.startswith("memory/"):
        return uri.rsplit("/", 1)[-1]
    return uri


def prune_orphan_rows(workspace: Path) -> list[str]:
    """Delete vector rows whose backing file (the ``path`` column) is gone.

    Scans the table for ``path`` values that no longer resolve to a file
    under ``workspace`` and drops those rows. Model-free — projects only
    ``id`` + ``path`` (no vectors) and deletes by id, so it never loads the
    embedding model.

    This catches orphans the FTS-driven staleness check is blind to: rows
    that exist ONLY in Lance and never in ``fts_meta`` (an out-of-band
    ``rm -rf memory/<dir>``, a reinstall, or an FTS-only rebuild). Rows
    with an empty ``path`` are left alone — they can't be verified, so
    deleting them would be unsafe. Returns the deleted ids.

    No-op (returns ``[]``) when lancedb is unavailable, the index dir is
    absent, the table is missing, or the table is empty.

    Sub-hazard B: dulwich reset --hard (in _fast_forward_working_tree) uses
    unlink-then-recreate, so a file may be transiently absent mid-reset.
    Candidates found absent on the initial scan are rechecked after acquiring
    the git-worktree lock (the same lock that serializes resets).  If a file
    is present after the lock is acquired the absence was transient and the row
    is kept.  Lock ordering: git-worktree (outer, acquired here) > LanceDB
    delete (inner, inside delete_ids).  No path takes a LanceDB lock and then
    git-worktree, so there is no deadlock risk.
    """
    try:
        import lancedb  # type: ignore[import-not-found]
    except ImportError:
        return []
    uri = str(Path(workspace).joinpath(*_INDEX_PATH))
    if not Path(uri).is_dir():
        return []
    db = lancedb.connect(uri)
    if _TABLE_NAME not in db.list_tables().tables:
        return []
    table = db.open_table(_TABLE_NAME)
    total = table.count_rows()
    if total == 0:
        return []
    ws = Path(workspace)
    rows = table.search().select(["id", "path"]).limit(total).to_list()
    # First pass: cheaply collect rows whose file appears absent.
    absent_rows = [
        r for r in rows
        if r.get("path") and not (ws / r["path"]).is_file()
    ]
    if not absent_rows:
        return []
    # Second pass: hold the git-worktree lock and re-check each candidate.
    # Any file that is present after we hold the lock was transiently absent
    # during a dulwich reset --hard; skip it.  Genuine orphans are still gone.
    from durin.memory.memory_writer import git_worktree_lock_path
    memory_git_root = ws / "memory"
    with cross_process_lock(git_worktree_lock_path(memory_git_root)):
        orphan_ids = [
            r["id"]
            for r in absent_rows
            if not (ws / r["path"]).is_file()
        ]
    if orphan_ids:
        delete_ids(workspace, orphan_ids)
    return orphan_ids


# Rebuild reads every current row into memory; past this many rows the
# fallback skips instead (the healthy optimize path has no such bound).
_REBUILD_MAX_ROWS = 50_000


def _vector_search_ok(table: Any) -> bool:
    """True when a real ANN probe succeeds — count_rows alone is NOT enough:
    lance 4.0's fragment rewrite can leave a table countable but unreadable
    by the vector path (Rust panic in lance-encoding on old-format data)."""
    try:
        dims = None
        for f in table.schema:
            if f.name == "vector":
                dims = f.type.list_size
        if not dims:
            return False
        table.search([0.0] * dims).limit(1).to_list()
        return True
    except Exception:  # noqa: BLE001 - any failure = unreadable
        return False


def compact_index(workspace: Path) -> dict:
    """Compact the vector table and drop superseded versions (best-effort).

    Every add/delete/upsert commits a new table version and nothing else
    ever prunes them; a churned table accretes thousands of tiny fragments
    and version manifests (2930 versions / 261MB of manifests for 22MB of
    data on one production workspace), taxing every open and search.

    ``optimize()`` on a table written by an older lance format can corrupt
    the VECTOR read path while ``count_rows`` still works (verified against
    that production table), so this is verify-or-rollback:

    1. ``optimize`` + probe search → done (healthy path).
    2. Probe fails → ``restore`` the pre-optimize version (search verified
       to recover), then REBUILD: re-create the table from the current
       rows — the rebuilt table is current-format, searchable, and
       optimizes cleanly from then on.

    The nightly dream calls this; failure is reported, never raised.
    Returns ``{"compacted", "mode", "versions_before", "versions_after",
    "duration_ms"}`` (plus ``"reason"`` when skipped/failed).
    """
    import time as _time

    try:
        import lancedb  # type: ignore[import-not-found]
    except ImportError:
        return {"compacted": False, "reason": "lancedb_unavailable"}
    uri = str(Path(workspace).joinpath(*_INDEX_PATH))
    if not Path(uri).is_dir():
        return {"compacted": False, "reason": "no_index"}
    t0 = _time.perf_counter()

    def _done(out: dict) -> dict:
        out["duration_ms"] = int((_time.perf_counter() - t0) * 1000)
        try:
            from durin.agent.tools._telemetry import emit_tool_event

            emit_tool_event("memory.index.compacted", dict(out))
        except Exception:  # pragma: no cover - telemetry best-effort
            pass
        return out

    try:
        db = lancedb.connect(uri)
        if _TABLE_NAME not in db.list_tables().tables:
            return {"compacted": False, "reason": "no_table"}
        from datetime import timedelta

        table = db.open_table(_TABLE_NAME)
        versions_before = len(table.list_versions())
        rows_before = table.count_rows()
        vector_dims = table.schema.field("vector").type.list_size
        v0 = table.version

        # Phase 1 keeps a week of versions so the pre-optimize version is
        # still there to restore if verification fails; phase 2 prunes the
        # rest only once the compacted table has proven readable. (Old lance
        # versions have no user-facing value here — markdown is the source
        # of truth and vectors are derivable — they are pure rollback fuel.)
        table.optimize(cleanup_older_than=timedelta(days=7))
        if _vector_search_ok(table) and table.count_rows() == rows_before:
            table.optimize(cleanup_older_than=timedelta(0))
            if _vector_search_ok(table):
                return _done({
                    "compacted": True, "mode": "optimized",
                    "versions_before": versions_before,
                    "versions_after": len(table.list_versions()),
                })

        # Fragment rewrite broke the vector read path — roll back.
        table.restore(v0)
        if not _vector_search_ok(table):
            return _done({
                "compacted": False,
                "reason": "verify_failed_and_restore_broken",
                "versions_before": versions_before,
            })
        if rows_before > _REBUILD_MAX_ROWS:
            return _done({
                "compacted": False, "reason": "rebuild_too_large",
                "versions_before": versions_before, "rows": rows_before,
            })

        # Rebuild from the (readable) current version: fresh format, one
        # fragment, one version. The vectors come along — no re-embedding.
        rows = table.search().limit(rows_before).to_list()
        for r in rows:
            r.pop("_distance", None)
        db.drop_table(_TABLE_NAME)
        # A table predating a schema addition (e.g. `entities`, B1) yields
        # rows missing that key; `pa.Table.from_pylist(rows, schema=...)`
        # fills it with null the way `create_table(data=rows, schema=...)`
        # itself does not (it requires the row dicts' key count to already
        # match the schema's column count).
        import pyarrow as pa

        rebuilt = db.create_table(
            _TABLE_NAME,
            data=pa.Table.from_pylist(rows, schema=_table_schema(vector_dims)),
        )
        if not _vector_search_ok(rebuilt) or rebuilt.count_rows() != rows_before:
            return _done({
                "compacted": False, "reason": "rebuild_verify_failed",
                "versions_before": versions_before,
            })
        return _done({
            "compacted": True, "mode": "rebuilt",
            "versions_before": versions_before,
            "versions_after": len(rebuilt.list_versions()),
        })
    except Exception as exc:  # noqa: BLE001 - maintenance must not fail a dream
        return _done({"compacted": False, "reason": f"error: {exc}"})
