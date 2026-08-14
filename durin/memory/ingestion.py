"""Ingest external artifacts (markdown / plain-text) as memory sources.

Pure file persistence. The LLM-derived fields in
``meta.json::derived`` (``summary``, ``entities``, ``relations``) are
populated later — either by dream over the ``ingested/`` directory or
by a follow-up ``memory_store`` call from the agent that just read the
file content.

Supported document formats (PDF, Office, EPUB, HTML, …) are converted to
markdown at ingest via :mod:`durin.memory.doc_convert`: the verbatim original
is kept at ``ingested/<id>/source.<ext>`` and the markdown rendering is written
alongside as ``ingested/<id>/source.md`` — the rendering is what becomes the
reference. Markdown and plain text are stored as-is. Formats markitdown does
not parse (e.g. ``.odt``, ``.rtf``, images) that are also non-utf-8 are
rejected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from durin.memory.paths import ingested_entry_dir
from durin.utils.atomic_write import atomic_write_text

__all__ = ["IngestError", "index_ingested_entry", "ingest_artifact"]

logger = logging.getLogger(__name__)


class IngestError(ValueError):
    """Raised when an artifact cannot be ingested."""


def ingest_artifact(
    workspace: Path,
    source_path: Path,
    *,
    documents_config=None,
    jobs=None,
    session_key: str | None = None,
) -> dict[str, Any]:
    """Copy a file into ``<workspace>/ingested/<id>/`` and persist meta.

    Returns a dict with: ``id``, ``source`` (path written), ``content``
    (utf-8 text), ``meta_path``, ``size_bytes``, ``job_id``, ``job_pages``.

    Idempotent: the same ``(filename, content)`` pair always resolves
    to the same ``id``, so re-ingesting the same file is a no-op.

    A PDF needing more OCR than the inline budget allows does not block:
    the original is stored right away, ``job_id`` names the background job
    transcribing it and ``job_pages`` is how many pages that job is expected
    to get through — an estimate, because deciding a document is over budget
    stops short of counting every page of it, and the worker settles the exact
    number itself. ``content`` is empty in that case, and the markdown sidecar —
    plus the Library entry indexed from it — is produced later, by the
    worker. ``documents_config`` is the ``DocumentsConfig`` to use (``None``
    loads it); ``jobs`` is the ``JobRegistry`` such a job is enqueued on
    (``None`` opens the default one); ``session_key`` tags the job with the
    session that requested it.
    """
    if not source_path.exists():
        raise IngestError(f"source does not exist: {source_path}")
    if not source_path.is_file():
        raise IngestError(f"source is not a regular file: {source_path}")

    from durin.memory.doc_convert import (
        DocConvertError,
        NeedsOcrJob,
        convert_file_to_markdown,
        is_convertible,
    )

    pending_ocr_pages: list[int] | None = None
    pending_ocr_total = 0
    converted = is_convertible(source_path.suffix)
    if converted:
        # A supported document (PDF/Office/EPUB/HTML/…): convert to markdown
        # for the reference, keep the original verbatim, key the id off the
        # ORIGINAL bytes so re-ingest stays idempotent for binaries.
        try:
            content = convert_file_to_markdown(
                source_path, documents_config=documents_config
            ).markdown
        except NeedsOcrJob as exc:
            # The text is not ready, but the document is: store the original
            # now and let a background job fill in the markdown sidecar. The
            # job is only spawned once the entry below is complete — its
            # worker reads the entry to learn what it is transcribing, and it
            # can be doing so before spawn even returns.
            content = ""
            # `pages` is the floor the conversion stopped confirming at, not
            # the whole set — how big the wait is comes from the estimate.
            pending_ocr_pages = exc.pages
            pending_ocr_total = exc.estimated_pages
        except DocConvertError as exc:
            raise IngestError(str(exc)) from exc
        entry_id = _bytes_id(source_path.name, source_path.read_bytes())
    else:
        try:
            content = source_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise IngestError(
                f"source is not utf-8 text and not a supported document format "
                f"(.odt, .rtf, images are unsupported): {source_path}"
            ) from exc
        entry_id = _content_id(source_path.name, content)

    size_bytes = source_path.stat().st_size

    entry_dir = ingested_entry_dir(workspace, entry_id)
    target = entry_dir / f"source{source_path.suffix or '.txt'}"
    if not target.exists():
        shutil.copy2(source_path, target)
    if converted and pending_ocr_pages is None:
        # A pending job means `content` is a placeholder, not a conversion —
        # the worker writes this sidecar itself once the real text exists.
        md_sidecar = entry_dir / "source.md"
        if not md_sidecar.exists():
            atomic_write_text(md_sidecar, content)

    meta_path = entry_dir / "meta.json"
    payload = {
        "id": entry_id,
        "derived": {
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "source_path": str(source_path),
            "size_bytes": size_bytes,
            # LLM-derived fields stay empty until dream or a
            # follow-up memory_store call fills them in.
            "summary": "",
            "entities": [],
            "relations": [],
        },
    }
    atomic_write_text(
        meta_path,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
    )

    job_id: str | None = None
    if pending_ocr_pages is not None:
        from durin.jobs.registry import JobRegistry
        from durin.jobs.spawn import spawn_ocr_job

        job = spawn_ocr_job(
            registry=jobs or JobRegistry(),
            pdf_path=target,
            pages=pending_ocr_pages,
            session_key=session_key,
            # `target` is the entry's normalized copy (source.pdf); the name
            # the user handed over is the only one that identifies the job in
            # a tray holding more than one of them.
            label=source_path.name,
            sidecar_dir=entry_dir,
            units_total=pending_ocr_total,
        )
        job_id = job.id

    return {
        "id": entry_id,
        "source": str(target),
        "content": content,
        "meta_path": str(meta_path),
        "size_bytes": size_bytes,
        "job_id": job_id,
        "job_pages": pending_ocr_total if pending_ocr_pages is not None else None,
    }


def index_ingested_entry(entry_dir: Path) -> str:
    """Publish a finished ingested entry to the Library and index it.

    An entry whose text arrived late — a scanned PDF transcribed page by page
    by a background job — gets its ``source.md`` written after the fact. This
    turns that markdown into the same searchable Library entry an ordinary
    ingest produces inline, titled after the file the user actually handed
    over rather than the normalized copy inside the entry. Returns the ref
    (``reference:<slug>``).

    Raises if the entry is incomplete (no markdown, no meta), if its markdown
    has no text at all — ``convert_file_to_markdown`` refuses that document
    inline, and a deferred one is no more useful with an empty Library entry
    behind it — or if the Library write fails. An entry that is not
    searchable is a job that did not finish its work.
    """
    entry_dir = Path(entry_dir)
    if entry_dir.parent.name != "ingested":
        raise IngestError(f"not an ingested entry directory: {entry_dir}")
    workspace = entry_dir.parent.parent

    markdown = (entry_dir / "source.md").read_text(encoding="utf-8")
    if not markdown.strip():
        raise IngestError(f"{entry_dir / 'source.md'} has no text to index")
    meta = json.loads((entry_dir / "meta.json").read_text(encoding="utf-8"))
    source_path = (meta.get("derived") or {}).get("source_path")
    if not source_path:
        raise IngestError(f"{entry_dir / 'meta.json'} has no derived.source_path")

    from durin.memory.reference import store_and_index_reference

    return store_and_index_reference(
        workspace,
        Path(source_path).stem,
        markdown,
        source=source_path,
        vector_index=_vector_index_for(workspace),
    )


def _vector_index_for(workspace: Path):
    """Build the workspace's vector index from the active config, or None.

    None when the semantic layer is off or its optional dependencies are not
    installed — the lexical half of search still runs, so this is a
    degradation, not a failure.
    """
    try:
        from durin.config.loader import load_config
        from durin.memory.vector_index import VectorIndex, vector_index_available

        cfg = load_config()
        if not cfg.memory.enabled or not vector_index_available():
            return None
        from durin.memory.embedding import provider_from_config

        return VectorIndex(
            workspace, provider_from_config(cfg, model=cfg.memory.embedding.model)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ingestion: vector index init failed: %s", exc)
        return None


def _content_id(filename: str, content: str) -> str:
    """Deterministic 12-char id from filename + content."""
    h = hashlib.sha256()
    h.update(filename.encode("utf-8"))
    h.update(b"\0")
    h.update(content.encode("utf-8"))
    return h.hexdigest()[:12]


def _bytes_id(filename: str, data: bytes) -> str:
    """Deterministic 12-char id from filename + raw bytes.

    Used for converted document sources so re-ingesting the same binary is a
    no-op regardless of any non-determinism in the markdown rendering.
    """
    h = hashlib.sha256()
    h.update(filename.encode("utf-8"))
    h.update(b"\0")
    h.update(data)
    return h.hexdigest()[:12]
