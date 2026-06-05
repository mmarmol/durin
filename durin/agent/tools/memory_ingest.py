"""memory_ingest tool — persist external artifacts as memory sources.

Phase 1.5 of the memory subsystem. The tool copies the source file to
``ingested/<id>/`` and, when memory is enabled, also creates a
corresponding ``memory/corpus/<id>.md`` entry plus a vector index
upsert so the document is searchable from the moment it's ingested —
not only after dream (Phase 3) runs over it. The body of the corpus
entry is a head excerpt of the ingested content (1500 chars, matching
the embed budget); full content stays in ``ingested/<id>/source.*``.

When memory is disabled, the tool falls back to just the file copy +
meta.json placeholder — the ``ingested/`` artifact is still grep-able
via ``memory_search(scope="undreamed")``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.memory.ingestion import IngestError, ingest_artifact
from durin.memory.provenance import author_scope
from durin.memory.storage import load_entry
from durin.memory.store import StoreError, store_memory
from durin.memory.vector_index import (
    VectorIndex,
    VectorIndexDimensionMismatchError,
    vector_index_available,
)

logger = logging.getLogger(__name__)

# How much of the ingested content we put in the body of the derived
# corpus entry. Matches the embed budget — anything bigger doesn't
# influence the embedding anyway and just wastes disk on duplication.
_CORPUS_BODY_BUDGET_CHARS = 1500

_PARAMETERS = tool_parameters_schema(
    path=StringSchema(
        "Absolute path (or workspace-relative path) to a markdown or "
        "plain-text file the user wants the agent to remember."
    ),
    required=["path"],
    description=(
        "Add a local document (markdown or plain text) to durin's memory as "
        "a REFERENCE — coherent source material the user wants kept whole: "
        "research notes, transcripts, technical specs, exported pages, "
        "markdown books, etc.\n\n"
        "`path` is the absolute or workspace-relative path to the file. The "
        "original is preserved verbatim and the document is indexed for "
        "retrieval. Re-ingesting the same file is idempotent — the id is a "
        "hash of (filename + content).\n\n"
        "For web content, use `web_fetch(url=...)` first to get clean "
        "markdown, then `memory_ingest` on the saved file. For a fact about a "
        "*thing* (a person, company, product, topic…), use "
        "`memory_upsert_entity` instead — `memory_ingest` is for whole "
        "documents, not individual facts."
    ),
)


@tool_parameters(_PARAMETERS)
class MemoryIngestTool(Tool):
    """memory_ingest tool — persist a document for later recall."""

    config_key = "memory"

    def __init__(
        self,
        workspace: str | Path,
        embedding_model: str | None = None,
        dream_config: Any | None = None,
        app_config: Any | None = None,
    ) -> None:
        self._workspace = Path(workspace).expanduser()
        self._embedding_model = embedding_model
        self._vector_index: Optional[VectorIndex] = None
        self._vector_index_attempted = False
        # Doc 25 §2.A.1 β.2 + P7 (doc 20): per-entity threshold trigger
        # config for post-ingest dream dispatch. None disables. See
        # ``durin.memory.threshold_trigger``.
        self._dream_config = dream_config
        # Full DurinConfig. Forwarded to the threshold trigger so the
        # spawned DreamRunner can resolve its model via aux_models.memory.
        self._app_config = app_config

    @property
    def name(self) -> str:
        return "memory_ingest"

    @property
    def description(self) -> str:
        # Canonical text per `docs/architecture/memory/06_prompts_and_instructions.md` §3.3.
        # Reads via `Tool.to_schema()` → `function.description` in the
        # OpenAI spec — what the LLM sees. Audit B1 (2026-05-28) caught
        # the prior short text drifted from the canonical doc.
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        # Read memory.* from the full DurinConfig on ctx.app_config —
        # ctx.config (= cfg.tools) does not carry a memory section.
        model = None
        dream_cfg = None
        app = getattr(ctx, "app_config", None)
        if app is not None:
            try:
                if app.memory.enabled:
                    model = app.memory.embedding.model
            except (AttributeError, TypeError):
                model = None
            try:
                dream_cfg = app.memory.dream
            except (AttributeError, TypeError):
                dream_cfg = None
        return cls(
            workspace=ctx.workspace,
            embedding_model=model,
            dream_config=dream_cfg,
            app_config=app,
        )

    def _get_vector_index(self) -> Optional[VectorIndex]:
        if self._vector_index_attempted:
            return self._vector_index
        self._vector_index_attempted = True
        if not self._embedding_model or not vector_index_available():
            return None
        try:
            from durin.memory.embedding import FastembedProvider

            provider = FastembedProvider(model=self._embedding_model)
            self._vector_index = VectorIndex(self._workspace, provider)
        except Exception as exc:
            logger.warning("vector index init failed: %s", exc)
            self._vector_index = None
        return self._vector_index

    async def execute(self, **kwargs: Any) -> Any:
        path_str = str(kwargs.get("path", "")).strip()
        if not path_str:
            return {"error": "path is required"}

        source = Path(path_str).expanduser()
        if not source.is_absolute():
            source = (self._workspace / source).resolve()

        try:
            result = ingest_artifact(self._workspace, source)
        except IngestError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"io error: {exc}"}

        emit_tool_event(
            "memory.ingest",
            {
                "entry_id": result["id"],
                "size_bytes": result["size_bytes"],
                "suffix": Path(result["source"]).suffix,
            },
        )

        # Derive a corpus memory entry pointing back to the ingested
        # source. This is what makes the document searchable via the
        # vector path. Best-effort: a failure to create the corpus
        # entry doesn't roll back the ingest.
        corpus_id = self._maybe_create_corpus_entry(
            source_path=source,
            ingested_path=result["source"],
            content=result["content"],
        )

        out = {
            "id": result["id"],
            "saved_to": result["source"],
            "meta_path": result["meta_path"],
            "size_bytes": result["size_bytes"],
            "content": result["content"],
        }
        if corpus_id:
            out["corpus_entry_id"] = corpus_id
        return out

    def _maybe_create_corpus_entry(
        self,
        *,
        source_path: Path,
        ingested_path: str,
        content: str,
    ) -> str | None:
        """Create a corpus memory entry derived from an ingested artifact.

        Returns the new entry id, or None if the derivation failed.
        Embeds + indexes the entry when the vector path is available.
        """
        ingested_rel = Path(ingested_path)
        try:
            ingested_rel = ingested_rel.relative_to(self._workspace)
        except ValueError:
            pass
        source_ref = f"[ingested {source_path.name}]({ingested_rel})"

        # P5.3: recursive character splitter — paragraph > line >
        # sentence > word > char preference, ~1500 chars per chunk
        # with ~200 chars overlap so a fact straddling a cut still
        # surfaces in both chunks.
        from durin.memory.text_splitter import split_text
        chunks = split_text(content) or [content[:_CORPUS_BODY_BUDGET_CHARS]]
        # Multi-chunk ingest: each chunk is its own corpus entry so
        # the search pipeline can rank them independently + the per-
        # source cap (doc 03 §12.4) limits top-K to 3 chunks per
        # source. We return the FIRST chunk's id for backward-compat
        # with callers that expect a single id.
        first_id: str | None = None
        for idx, chunk in enumerate(chunks):
            try:
                with author_scope("agent_created"):
                    stored = store_memory(
                        self._workspace,
                        content=chunk,
                        class_name="corpus",
                        headline=(
                            f"Ingested: {source_path.name}"
                            + (f" (chunk {idx + 1}/{len(chunks)})"
                               if len(chunks) > 1 else "")
                        ),
                        summary="",
                        source_refs=[source_ref],
                    )
            except (StoreError, OSError) as exc:
                logger.warning(
                    "memory_ingest: chunk %d/%d failed for %s: %s",
                    idx + 1, len(chunks), source_path.name, exc,
                )
                continue
            if first_id is None:
                first_id = stored["id"]

            vi = self._get_vector_index()
            if vi is not None:
                try:
                    entry_path = Path(stored["path"])
                    entry = load_entry(entry_path)
                    vi.upsert(entry, stored["class"], entry_path)
                except VectorIndexDimensionMismatchError as exc:
                    logger.warning("ingest vector upsert: %s", exc)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "ingest vector upsert failed for %s: %s",
                        stored["id"], exc,
                    )

            try:
                from durin.memory.indexer import reindex_one_file
                reindex_one_file(
                    self._workspace, Path(stored["path"]),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "memory_ingest FTS reindex failed for %s: %s",
                    stored["id"], exc,
                )

        if first_id is None:
            # Every chunk failed; nothing to emit, return None to
            # signal the failure.
            return None

        emit_tool_event(
            "memory.store",
            {
                "entry_id": stored["id"],
                "class_name": stored["class"],
                "author": stored["author"],
                "headline": stored["headline"],
            },
        )

        # §8e: the post-ingest threshold trigger (legacy DreamRunner dispatch)
        # is removed. Ingested material is searchable via FTS + vector and gets
        # processed by the daily extract/refine passes.
        return first_id
