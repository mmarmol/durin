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

        # §A2: store the ingested document WHOLE as a reference (design §2.3/§2.8)
        # — FTS the whole doc + vector-index its token-aware chunks. Replaces the
        # legacy chunked `corpus/` model. Best-effort: a failure here does not
        # roll back the verbatim ingest above.
        ref = self._create_reference(source_path=source, content=result["content"])

        out = {
            "id": result["id"],
            "saved_to": result["source"],
            "meta_path": result["meta_path"],
            "size_bytes": result["size_bytes"],
            "content": result["content"],
        }
        if ref:
            out["reference"] = ref
        return out

    def _create_reference(self, *, source_path: Path, content: str) -> str | None:
        """Store the ingested document as a reference (whole doc + token-aware
        chunk index), FTS-index the whole doc, and vector-index each chunk.

        Returns the ref (``reference:<slug>``) or None on failure. The whole doc
        is the FTS unit; the chunks (each <=512 tok, parent-pointed) are the
        vector unit (design A2 / reference.py).
        """
        from durin.memory.reference import ingest_reference, reference_chunks
        try:
            res = ingest_reference(
                self._workspace, source_path.stem, content,
                source=str(source_path),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "memory_ingest: reference write failed for %s: %s",
                source_path.name, exc,
            )
            return None
        ref = res.ref
        slug = ref.split(":", 1)[1]
        ref_md = self._workspace / "memory" / "references" / f"{slug}.md"

        # FTS: the whole reference doc is the lexical unit (indexer._payload_for).
        try:
            from durin.memory.indexer import reindex_one_file
            reindex_one_file(self._workspace, ref_md)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "memory_ingest: reference FTS reindex failed for %s: %s", ref, exc,
            )

        # Vector: each token-aware chunk, keyed <ref>#<idx> so a fragment hit
        # resolves to the parent reference.
        vi = self._get_vector_index()
        if vi is not None:
            for rec in reference_chunks(self._workspace, ref):
                try:
                    vi.upsert_reference_chunk(
                        ref=ref, idx=rec["idx"], text=rec["text"], path=ref_md,
                    )
                except VectorIndexDimensionMismatchError as exc:
                    logger.warning("ingest reference vector upsert: %s", exc)
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "ingest reference vector upsert failed for %s#%s: %s",
                        ref, rec.get("idx"), exc,
                    )
        return ref
