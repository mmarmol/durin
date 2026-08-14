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
from durin.memory.pdf_coverage import is_pre_flag_stub_note
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

    ``entry_id`` is computed first, before any conversion runs, so a
    re-ingest of the same ``(filename, bytes)`` pair is decided by the
    entry's own state on disk rather than by re-running the work:

    - Finished with a real transcription (``source.md`` there, and
      ``meta.json``'s ``derived.ocr_stub`` false): never redone. Conversion
      is skipped and ``content`` comes back from that file, ``job_id=None``.
    - A ``meta.json`` predating the flag (no ``derived.ocr_stub`` key at
      all — flag-less versions never wrote one): the sidecar is classified
      once by the frozen note heads those versions prepended to every stub,
      the verdict is persisted back into ``meta.json`` with nothing else in
      it changed, and the entry then behaves as whichever case it really is.
    - Finished with an OCR-off/engine-missing STUB (``derived.ocr_stub`` is
      true) while OCR still cannot do better: returned as-is, honestly —
      the note is still the current answer, not a stale one.
    - The same stub, but OCR is now enabled and the engine is now available:
      not returned as-is. Conversion runs again — the situation that made it
      a stub has changed, so the next re-ingest re-checks instead of
      trusting a note that may now be wrong. The sidecar and the flag are
      refreshed on disk, not just in this call's return value.
    - A background OCR job for it is still ``queued``/``running`` (tracked by
      the entry's ``ocr_job.json`` marker): also skipped, returning that SAME
      ``job_id`` instead of spawning a second one.
    - The marker names a job that is ``failed``/``cancelled``, or one the
      registry no longer has: a legitimate retry — conversion runs again and
      a fresh job is spawned, overwriting the marker.
    - A half state (``source.md`` present but ``meta.json`` missing — the two
      writes are each atomic but not one transaction, so a crash between them
      is possible) is treated as no finished entry at all: conversion runs
      again and both files are rebuilt.

    A PDF needing more OCR than the inline budget allows does not block:
    the original is stored right away, ``job_id`` names the background job
    transcribing it and ``job_pages`` is how many pages that job is expected
    to get through — an estimate, because deciding a document is over budget
    stops short of counting every page of it, and the worker settles the exact
    number itself. ``content`` is empty in that case, and the markdown sidecar —
    plus the Library entry indexed from it — is produced later, by the
    worker. ``documents_config`` is the ``DocumentsConfig`` to use (``None``
    loads it); ``jobs`` is the ``JobRegistry`` used for both a spawned job
    (enqueued on it) and an existing one named by the marker (looked up in
    it) (``None`` opens the default one); ``session_key`` tags a freshly
    spawned job with the session that requested it.
    """
    if not source_path.exists():
        raise IngestError(f"source does not exist: {source_path}")
    if not source_path.is_file():
        raise IngestError(f"source is not a regular file: {source_path}")

    from durin.memory.doc_convert import (
        DocConvertError,
        NeedsOcrJob,
        convert_file_to_markdown,
        engine_available,
        is_convertible,
    )

    # Identity first, before anything that could be a conversion. A converted
    # document keys off its raw bytes (legal to read before deciding anything
    # about them) so re-ingest stays idempotent regardless of any
    # non-determinism in the markdown rendering; a plain text/markdown source
    # keys off its decoded content, which reading it for identity already
    # required — that read is not the expensive "conversion" step the
    # short-circuits below exist to skip.
    converted = is_convertible(source_path.suffix)
    if converted:
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
    meta_path = entry_dir / "meta.json"
    md_sidecar = entry_dir / "source.md"

    # A sidecar alone does not prove the entry is finished and final: it can
    # be an OCR-off/engine-missing STUB (a coverage note, not a real
    # transcription -- see ConvertedDoc.ocr_stub), and the two writes below
    # (sidecar, then meta.json) are each individually atomic but not one
    # transaction, so a crash between them is a real state to handle, not a
    # hypothetical. `sidecar_needs_rewrite` tracks the two cases that fall
    # through this block without returning: rebuild-both (meta missing) and
    # upgrade-in-place (stub, OCR can now do better) both mean the eventual
    # write below must overwrite rather than skip because the file exists.
    sidecar_needs_rewrite = False
    if md_sidecar.exists():
        if not meta_path.exists():
            # Half state: something crashed between the sidecar write and
            # the meta write. Trusting a sidecar with no meta to verify it
            # against is exactly the "no-op forever" trap re-ingest must not
            # fall into -- treat it as no finished entry at all.
            sidecar_needs_rewrite = True
        else:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            sidecar_content = md_sidecar.read_text(encoding="utf-8")
            derived = meta.get("derived") or {}
            if "ocr_stub" in derived:
                is_stub = bool(derived["ocr_stub"])
            else:
                # meta.json predates the flag: flag-less versions wrote no
                # key at all, so an absent key is "no verdict recorded", not
                # "not a stub" -- reading it as False replayed a stale stub
                # note forever for every entry those versions ingested. The
                # sidecar's own first bytes are the remaining evidence:
                # classify it once by the frozen pre-flag note heads and
                # persist the verdict so the sniff never re-runs. The write
                # is surgical -- the loaded dict round-trips with just this
                # key added, so ingested_at and any dream-derived summary/
                # entities/relations ride along untouched.
                is_stub = is_pre_flag_stub_note(sidecar_content)
                derived["ocr_stub"] = is_stub
                meta["derived"] = derived
                atomic_write_text(
                    meta_path,
                    json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=False),
                )
            upgrade_available = False
            if is_stub:
                if documents_config is None:
                    from durin.config.loader import load_config

                    documents_config = load_config().documents
                upgrade_available = documents_config.ocr.enabled and engine_available()
            if not is_stub or not upgrade_available:
                # Either a real transcription (never redone) or a stub OCR
                # still cannot improve on (returned honestly, as the current
                # answer, not a stale one) -- both are the correct response
                # right now, so meta.json is not rewritten with a fresh
                # payload: that would reset ingested_at and, for a document a
                # dream pass has since distilled, wipe its derived summary/
                # entities/relations back to empty on every casual
                # re-mention. (The legacy resolve above adds one key to the
                # loaded dict precisely so everything else survives.)
                return {
                    "id": entry_id,
                    "source": str(target),
                    "content": sidecar_content,
                    "meta_path": str(meta_path),
                    "size_bytes": size_bytes,
                    "job_id": None,
                    "job_pages": None,
                }
            # A stub, and OCR can now do better: fall through and re-convert,
            # overwriting the stale note instead of replaying it.
            sidecar_needs_rewrite = True

    registry = jobs
    ocr_job_marker = entry_dir / "ocr_job.json"
    if ocr_job_marker.exists():
        from durin.jobs.registry import JobRegistry

        registry = registry or JobRegistry()
        marker_job_id = json.loads(ocr_job_marker.read_text(encoding="utf-8"))["job_id"]
        existing_job = registry.get(marker_job_id)
        # Branch on the row's CURRENT status only, never on whether it was
        # ever failed: a job later revived from failed back to queued by some
        # other action must read as pending here, exactly like one that was
        # always queued, or the two would fight over the same row. A vanished
        # row (get() -> None -- terminal jobs can be pruned), a
        # failed/cancelled one, and a `done` one all fall through to the same
        # retry below. `done` is not the ordinary race it sounds like; it
        # arrives here on two routes. Either the sidecar its worker wrote
        # (always before calling finish(), see durin/jobs/ocr_worker.py) is
        # no longer there -- removed after the fact, not by the worker's own
        # sequencing -- or the sidecar IS there and the block above fell
        # through instead of returning: no meta.json to verify it against,
        # or a stub OCR can now upgrade. Retrying is the safe, self-healing
        # response on every route.
        if existing_job is not None and existing_job.status in ("queued", "running"):
            return {
                "id": entry_id,
                "source": str(target),
                "content": "",
                "meta_path": str(meta_path),
                "size_bytes": size_bytes,
                "job_id": existing_job.id,
                "job_pages": existing_job.units_total,
            }

    pending_ocr_pages: list[int] | None = None
    pending_ocr_total = 0
    is_ocr_stub = False
    if converted:
        # A supported document (PDF/Office/EPUB/HTML/…): convert to markdown
        # for the reference, keep the original verbatim.
        try:
            converted_doc = convert_file_to_markdown(
                source_path, documents_config=documents_config
            )
            content = converted_doc.markdown
            is_ocr_stub = converted_doc.ocr_stub
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
    # else: `content` was already read above, decoding entry_id's identity;
    # a plain text/markdown source has no OCR-stub concept, is_ocr_stub stays
    # False.

    if not target.exists():
        shutil.copy2(source_path, target)
    if converted and pending_ocr_pages is None:
        # A pending job means `content` is a placeholder, not a conversion —
        # the worker writes this sidecar itself once the real text exists.
        # sidecar_needs_rewrite (rebuilding a half state, or upgrading a
        # stub) means the file has to be overwritten even though it already
        # exists -- an existing sidecar is otherwise trusted and left alone.
        if not md_sidecar.exists() or sidecar_needs_rewrite:
            atomic_write_text(md_sidecar, content)

    payload = {
        "id": entry_id,
        "derived": {
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "source_path": str(source_path),
            "size_bytes": size_bytes,
            # True when `content` is an OCR-off/engine-missing coverage note
            # rather than a real transcription -- read back by the
            # short-circuit above to decide whether a future re-ingest can
            # trust this sidecar or should re-check it.
            "ocr_stub": is_ocr_stub,
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
            registry=registry or JobRegistry(),
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
        # Written strictly after a successful spawn, so a marker never names
        # a job that does not exist. This is still a check-then-act across
        # processes, not an atomic reservation: two ingests of the identical
        # bytes that both observe no marker/sidecar before either one's
        # spawn+write lands can both spawn. The window is narrow (a
        # probe/confirm pass, not a full OCR run) and its cost is bounded --
        # two queued rows for the same book, which the existing concurrency
        # cap and chaining drain one after another without corrupting
        # anything. What this fixes is the guaranteed-every-time duplicate
        # from a plain sequential re-ingest, not this rare simultaneous race.
        atomic_write_text(
            ocr_job_marker,
            json.dumps({"job_id": job_id}, indent=2),
        )

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
