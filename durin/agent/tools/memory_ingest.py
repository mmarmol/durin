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
from durin.memory.store import StoreError, store_memory
from durin.memory.storage import load_entry
from durin.memory.vector_index import (
    VectorIndex,
    VectorIndexDimensionMismatch,
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
        "Persist a user-supplied document into the agent's memory store. "
        "Markdown and plain-text only."
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
    ) -> None:
        self._workspace = Path(workspace).expanduser()
        self._embedding_model = embedding_model
        self._vector_index: Optional[VectorIndex] = None
        self._vector_index_attempted = False
        # Doc 25 §2.A.1 β.2 + P7 (doc 20): per-entity threshold trigger
        # config for post-ingest dream dispatch. None disables. See
        # ``durin.memory.threshold_trigger``.
        self._dream_config = dream_config

    @property
    def name(self) -> str:
        return "memory_ingest"

    @property
    def description(self) -> str:
        return (
            "Persist a markdown or plain-text file into the agent's memory "
            "store. The file is copied to a stable location keyed by content "
            "hash; a derived corpus memory entry is created so the document "
            "is searchable immediately (full content stays in the original "
            "ingested file, accessible via memory_drill). The file's content "
            "is returned in the result so the agent can read it in the same "
            "turn."
        )

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
            ingested_id=result["id"],
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
        ingested_id: str,
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
        body = content[:_CORPUS_BODY_BUDGET_CHARS]

        try:
            with author_scope("agent_created"):
                stored = store_memory(
                    self._workspace,
                    content=body,
                    class_name="corpus",
                    headline=f"Ingested: {source_path.name}",
                    summary="",
                    source_refs=[source_ref],
                )
        except (StoreError, OSError) as exc:
            logger.warning(
                "memory_ingest: derived corpus entry failed for %s: %s",
                source_path.name, exc,
            )
            return None

        vi = self._get_vector_index()
        if vi is not None:
            try:
                entry_path = Path(stored["path"])
                entry = load_entry(entry_path)
                vi.upsert(entry, stored["class"], entry_path)
            except VectorIndexDimensionMismatch as exc:
                logger.warning("ingest vector upsert: %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ingest vector upsert failed for %s: %s",
                    stored["id"], exc,
                )

        emit_tool_event(
            "memory.store",
            {
                "entry_id": stored["id"],
                "class_name": stored["class"],
                "author": stored["author"],
                "headline": stored["headline"],
            },
        )

        # P7 (doc 20): post-ingest threshold trigger. Reuses the same
        # shared helper as memory_store. The corpus entry itself has
        # no entity tags today (memory_ingest doesn't extract entities
        # — that's G1 territory). The trigger only fires if a future
        # change tags the corpus entry; in the meantime this is a
        # no-op call that keeps the wiring in place and symmetrical
        # with memory_store.
        try:
            from durin.memory.storage import load_entry as _load_entry
            from durin.memory.threshold_trigger import (
                maybe_dispatch_threshold_dream,
            )

            entry = _load_entry(Path(stored["path"]))
            entities = list(entry.entities or ())
            if entities:
                maybe_dispatch_threshold_dream(
                    workspace=self._workspace,
                    entities=entities,
                    dream_config=self._dream_config,
                    vector_index=vi,
                    source_trigger="post_ingest_threshold",
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "post_ingest_threshold dispatch failed for %s",
                stored.get("id"),
            )

        return stored["id"]
