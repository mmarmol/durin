"""Shared helpers for decoding ``data:...;base64,...`` URLs to disk.

Historically lived in ``durin.api.server``; now shared by the WebSocket
channel so the ``api`` + ``websocket`` ingress paths apply the same parsing,
size guard, and filesystem layout.
"""

from __future__ import annotations

import base64
import mimetypes
import re
import uuid
from pathlib import Path

from durin.utils.helpers import safe_filename

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
MAX_FILE_SIZE = DEFAULT_MAX_BYTES

# Tolerate media-type parameters (e.g. ``;codecs=opus`` from MediaRecorder)
# between the MIME and ``;base64``. Group 1 is the base ``type/subtype``;
# params are matched and discarded. A stricter regex silently dropped recorded
# ``audio/webm;codecs=opus`` uploads (the upload chip spun forever).
_DATA_URL_RE = re.compile(r"^data:([^;,]+)(?:;[\w.+-]+=[^;,]*)*;base64,(.+)$", re.DOTALL)


class FileSizeExceeded(Exception):  # noqa: N818 — deliberate event-style name, not *Error
    """Raised when a decoded payload exceeds the caller's size limit."""


def save_base64_data_url(
    data_url: str,
    media_dir: Path,
    *,
    max_bytes: int | None = None,
    filename_hint: str | None = None,
) -> str | None:
    """Decode a ``data:<mime>;base64,<payload>`` URL and persist it.

    Returns the absolute path on success, ``None`` when the URL shape or the
    base64 payload itself is malformed. Raises :class:`FileSizeExceeded`
    when the decoded payload is larger than ``max_bytes`` (default 10 MB).

    ``filename_hint`` (the client-supplied original name) supplies the saved
    file's extension when present — needed for documents, whose tool dispatch
    (``convert_to_markdown`` / ``memory_ingest``) keys off the suffix and whose
    MIME (docx, epub, …) ``mimetypes.guess_extension`` does not reliably know.
    """
    m = _DATA_URL_RE.match(data_url)
    if not m:
        return None
    mime_type, b64_payload = m.group(1), m.group(2)
    try:
        raw = base64.b64decode(b64_payload)
    except Exception:
        return None
    limit = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    if len(raw) > limit:
        raise FileSizeExceeded(f"File exceeds {limit // (1024 * 1024)}MB limit")
    ext = ""
    if filename_hint:
        ext = Path(filename_hint).suffix.lower()
    if not ext:
        ext = mimetypes.guess_extension(mime_type) or ".bin"
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    dest = media_dir / safe_filename(filename)
    dest.write_bytes(raw)
    return str(dest)
