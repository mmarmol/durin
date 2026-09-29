"""Shared helpers for decoding ``data:...;base64,...`` URLs to disk.

Shared by the OpenAI-compatible ``/v1`` routes and the WebSocket channel so
both ingress paths apply the same parsing, size guard, and filesystem layout.
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


# The extension each accepted audio MIME type is saved with. The platform's
# ``mimetypes.guess_extension`` has no answer for some of them, and its answer
# for others varies; saved as ``.bin`` (or ``.webm``, read as video), a
# recording would no longer read as audio to the agent loop.
_AUDIO_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/webm": ".weba",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
}


class FileSizeExceeded(Exception):  # noqa: N818 — deliberate event-style name, not *Error
    """Raised when a decoded payload exceeds the caller's size limit."""


# What a sender's file name may keep: line breaks, control characters and
# path characters become dashes, since the name reaches the agent's text and
# a file path.
_NAME_UNSAFE = re.compile(r'[\s\x00-\x1f\x7f<>:"/\\|?*]+')
_NAME_MAX_BYTES = 120


def _name_stem(name: str) -> str:
    """The sender's file name without its extension, safe for a file name and
    a line of text, cut to a bounded size."""
    stem = _NAME_UNSAFE.sub("-", Path(name).stem).strip("-._")
    return stem.encode("utf-8")[:_NAME_MAX_BYTES].decode("utf-8", errors="ignore")


def save_base64_data_url(
    data_url: str,
    media_dir: Path,
    *,
    max_bytes: int | None = None,
    name: str | None = None,
    name_sets_extension: bool = False,
) -> str | None:
    """Decode a ``data:<mime>;base64,<payload>`` URL and persist it.

    Returns the absolute path on success, ``None`` when the URL shape or the
    base64 payload itself is malformed. Raises :class:`FileSizeExceeded`
    when the decoded payload is larger than ``max_bytes`` (default 10 MB).

    ``name`` (the sender's original file name) is kept in the saved name,
    after a unique prefix: the agent refers to a file by its saved name, so a
    random one hides which of the user's files it is. The extension comes from
    the MIME, unless ``name_sets_extension`` — documents, whose tool dispatch
    (``convert_to_markdown`` / ``memory_ingest``) keys off the suffix and whose
    MIME (docx, epub, …) ``mimetypes.guess_extension`` does not reliably know.
    An image or a recording is read by its content type, so its name never
    changes the extension.
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
    if name and name_sets_extension:
        ext = Path(name).suffix.lower()
    if not ext:
        ext = (
            _AUDIO_EXTENSIONS.get(mime_type)
            or mimetypes.guess_extension(mime_type)
            or ".bin"
        )
    stem = _name_stem(name) if name else ""
    filename = f"{uuid.uuid4().hex[:12]}_{stem}{ext}" if stem else f"{uuid.uuid4().hex[:12]}{ext}"
    dest = media_dir / safe_filename(filename)
    dest.write_bytes(raw)
    return str(dest)
