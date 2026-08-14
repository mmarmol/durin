"""memory_ingest tool — persist external artifacts as memory sources.

The tool copies the source file to ``ingested/<id>/`` and, when memory is
enabled, stores the document WHOLE as a reference
(``memory/references/<slug>.md`` + a token-aware ``.chunks.jsonl`` sidecar) and
indexes it (FTS + vector) so it's searchable the moment it's ingested — not
only after a dream pass. This replaced the legacy chunked ``corpus/`` model;
full content stays in ``ingested/<id>/source.*``.

A scanned PDF over the inline OCR budget is the one case where the text does
not exist yet at this point. There is nothing to store as a reference, so the
tool creates none and reports the background job instead; the worker that
transcribes the pages stores and indexes the document when it is done.

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
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.memory.ingestion import IngestError, ingest_artifact
from durin.memory.vector_index import VectorIndex, vector_index_available

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    path=StringSchema(
        "Absolute path (or workspace-relative path) to the document the user "
        "wants the agent to remember."
    ),
    required=["path"],
    description=(
        "Add a local document to durin's memory as a REFERENCE — coherent "
        "source material the user wants kept whole: research notes, "
        "transcripts, technical specs, exported pages, books, reports, etc.\n\n"
        "Supported formats are converted to markdown on the way in: PDF, Word "
        "(docx), PowerPoint (pptx), Excel (xlsx/xls), EPUB, HTML, CSV, JSON, "
        "XML, Jupyter notebooks; markdown and plain text are stored as-is. The "
        "verbatim original is always preserved.\n\n"
        "`path` is the absolute or workspace-relative path to the file. "
        "Re-ingesting the same file is idempotent. The result includes a "
        "`reference:<slug>`; when you then author an entity distilled from this "
        "document, pass that ref in `memory_upsert_entity(derived_from=[...])` "
        "so the entity links back to its source.\n\n"
        "One case returns no text and no reference: a scanned PDF with more "
        "pages than can be transcribed on the spot. The result carries "
        "`job_id`, `pages_pending` and a `note` instead, and the document is "
        "neither readable nor searchable until that background job finishes. "
        "Say so — do not report it as read or remembered — and use `tasks` to "
        "report progress.\n\n"
        "For web content, use `web_fetch(url=...)` first, then `memory_ingest` "
        "on the saved file. For a fact about a *thing* (a person, company, "
        "product, topic…), use `memory_upsert_entity` instead — `memory_ingest` "
        "is for whole documents, not individual facts. To just READ a document "
        "in this turn without saving it, use `convert_to_markdown`."
    ),
)


@tool_parameters(_PARAMETERS)
class MemoryIngestTool(Tool, ContextAware):
    """memory_ingest tool — persist a document for later recall."""

    _scopes = {"core", "subagent"}

    config_key = "memory"

    def __init__(
        self,
        workspace: str | Path,
        embedding_model: str | None = None,
    ) -> None:
        self._workspace = Path(workspace).expanduser()
        self._embedding_model = embedding_model
        self._vector_index: Optional[VectorIndex] = None
        self._vector_index_attempted = False
        self._request_ctx: RequestContext | None = None

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx = ctx

    def _session_key(self) -> str | None:
        # Mirrors TasksTool._session_key: an ingest that triggers a background
        # OCR job must tag it with the requesting session, or the job's
        # session_key stays NULL and it is invisible to every session's tray.
        ctx = self._request_ctx
        if ctx is None:
            return None
        if ctx.session_key:
            return ctx.session_key
        if ctx.channel and ctx.chat_id:
            return f"{ctx.channel}:{ctx.chat_id}"
        return None

    @property
    def name(self) -> str:
        return "memory_ingest"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        # Read memory.* from the full DurinConfig on ctx.app_config —
        # ctx.config (= cfg.tools) does not carry a memory section.
        model = None
        app = getattr(ctx, "app_config", None)
        if app is not None:
            try:
                if app.memory.enabled:
                    model = app.memory.embedding.model
            except (AttributeError, TypeError):
                model = None
        return cls(
            workspace=ctx.workspace,
            embedding_model=model,
        )

    def _get_vector_index(self) -> Optional[VectorIndex]:
        if self._vector_index_attempted:
            return self._vector_index
        self._vector_index_attempted = True
        if not self._embedding_model or not vector_index_available():
            return None
        try:
            from durin.config.loader import load_config
            from durin.memory.embedding import provider_from_config

            provider = provider_from_config(load_config(), model=self._embedding_model)
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
            # Off-loop: ingest converts the document (MarkItDown: PDF/Office
            # parsing) — multi-second blocking work that would freeze the gateway
            # loop for large docs.
            import asyncio
            result = await asyncio.to_thread(
                ingest_artifact, self._workspace, source,
                session_key=self._session_key(),
            )
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

        # Store the ingested document WHOLE as a reference — FTS the whole
        # doc + vector-index its token-aware chunks. Replaces the legacy
        # chunked `corpus/` model. Best-effort: a failure here does not
        # roll back the verbatim ingest above.
        #
        # Skipped entirely when a background OCR job owns this document: its
        # `content` is an empty placeholder, and storing that would put an
        # empty entry in the Library, hand the agent a ref that resolves to
        # nothing, and leave it there for good if the job later fails. The
        # worker stores and indexes the real text when it has it.
        #
        # Off-loop: reference creation FTS-indexes the whole doc + vector-indexes
        # its chunks (embedding) — blocking, must not run on the gateway loop.
        job_id = result.get("job_id")
        ref = None
        if not job_id:
            ref = await asyncio.to_thread(
                self._create_reference, source_path=source, content=result["content"])

        # C1: emit `id` + `reference` FIRST so they survive the 16 KB head
        # truncation on large docs — the agent (and the dream) read the
        # `reference:<slug>` to link the entity back to its source. `content`
        # (the whole doc) goes last for the same reason. The pending-job keys
        # lead for the same reason: they are what the agent must act on.
        out: dict[str, Any] = {"id": result["id"]}
        if ref:
            out["reference"] = ref
        if job_id:
            out["job_id"] = job_id
            out["pages_pending"] = result["job_pages"]
            out["note"] = (
                "No text yet: this scanned document's pages are being "
                "transcribed by a background job. It is not readable or "
                "searchable until that finishes — tell the user that instead "
                "of reporting the document as read or remembered. `tasks` "
                "reports the progress."
            )
        out["saved_to"] = result["source"]
        out["meta_path"] = result["meta_path"]
        out["size_bytes"] = result["size_bytes"]
        out["content"] = result["content"]
        return out

    def _create_reference(self, *, source_path: Path, content: str) -> str | None:
        """Store the ingested document as a searchable reference.

        Shares :func:`durin.memory.reference.store_and_index_reference` with
        the OCR worker, which does this for a document whose text only exists
        once its background transcription finishes — one implementation, so
        an inline ingest and a deferred one cannot end up indexed differently.

        Returns the ref (``reference:<slug>``) or None on failure: the
        verbatim ingest already succeeded and is not rolled back for this.
        """
        from durin.memory.reference import store_and_index_reference

        try:
            return store_and_index_reference(
                self._workspace, source_path.stem, content,
                source=str(source_path),
                vector_index=self._get_vector_index(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "memory_ingest: reference write failed for %s: %s",
                source_path.name, exc,
            )
            return None
