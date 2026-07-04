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
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from durin.memory.paths import ingested_entry_dir
from durin.utils.atomic_write import atomic_write_text

__all__ = ["IngestError", "ingest_artifact"]


class IngestError(ValueError):
    """Raised when an artifact cannot be ingested."""


def ingest_artifact(workspace: Path, source_path: Path) -> dict[str, Any]:
    """Copy a file into ``<workspace>/ingested/<id>/`` and persist meta.

    Returns a dict with: ``id``, ``source`` (path written), ``content``
    (utf-8 text), ``meta_path``, ``size_bytes``.

    Idempotent: the same ``(filename, content)`` pair always resolves
    to the same ``id``, so re-ingesting the same file is a no-op.
    """
    if not source_path.exists():
        raise IngestError(f"source does not exist: {source_path}")
    if not source_path.is_file():
        raise IngestError(f"source is not a regular file: {source_path}")

    from durin.memory.doc_convert import (
        DocConvertError,
        convert_file_to_markdown,
        is_convertible,
    )

    converted = is_convertible(source_path.suffix)
    if converted:
        # A supported document (PDF/Office/EPUB/HTML/…): convert to markdown
        # for the reference, keep the original verbatim, key the id off the
        # ORIGINAL bytes so re-ingest stays idempotent for binaries.
        try:
            content = convert_file_to_markdown(source_path).markdown
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
    if converted:
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

    return {
        "id": entry_id,
        "source": str(target),
        "content": content,
        "meta_path": str(meta_path),
        "size_bytes": size_bytes,
    }


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
