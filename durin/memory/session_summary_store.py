"""Session summaries as markdown projections under `memory/session_summary/`.

Audit A10 (2026-05-28). Pre-A10, session summaries lived as
`session.metadata["_last_summary"] = {"text", "last_active"}` inside
`<session_key>.meta.json`. The hot layer read them directly from
the in-memory dict; nothing indexed them. Doc 02 §3.3 promised
indexing but the walker only iterates `.md` under `memory/`.

A10 picks the single-source-of-truth path (per A4 lessons): the
summary lives ONLY in `memory/session_summary/<sanitized_key>.md`.
The JSON metadata stops carrying `_last_summary` going forward.
This module owns the write / read / sanitize and the one-shot
migration of pre-A10 sessions that still have the legacy field.

Why this module instead of inlining the file I/O in `agent/memory.py`:

- The same read path is needed by the hot layer (`agent/memory.py`
  + `agent/loop.py`) and indirectly by the search pipeline (via the
  walker → indexer → `class_name="session_summary"`).
- Sanitisation of session keys (which can contain ``:`` or other
  unsafe chars on channels like ``telegram:<id>``) lives in one
  place.
- The migration helper keeps backward compatibility silent — old
  sessions with `_last_summary` in the JSON keep working until they
  next compact.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple

from durin.memory.paths import memory_class_dir
from durin.memory.schema import MemoryEntry
from durin.memory.storage import load_entry, save_entry

logger = logging.getLogger(__name__)

__all__ = [
    "SESSION_SUMMARY_CLASS",
    "delete_session_summary",
    "get_session_summary",
    "sanitize_session_key",
    "session_summary_path",
    "write_session_summary",
]


SESSION_SUMMARY_CLASS = "session_summary"

# Same shape as `TelemetryLogger`'s key sanitiser: collapse anything
# that isn't a word char or dash into `_`, drop runs of dots so a
# path-like key can't escape the directory. Cap at 80 chars to keep
# the filename in any filesystem's name limit.
_SAFE_KEY_RE = re.compile(r"[^\w\-]")
_DOT_RUN_RE = re.compile(r"\.{2,}")


def sanitize_session_key(key: str) -> str:
    """Map an arbitrary session key to a filename-safe identifier.

    Mirrors the convention used by ``durin.telemetry.logger
    .get_session_logger`` so a single session key sanitises the same
    way wherever the system touches it.
    """
    safe = _SAFE_KEY_RE.sub("_", key)[:80]
    safe = _DOT_RUN_RE.sub("_", safe)
    return safe or "default"


def session_summary_path(workspace: Path, session_key: str) -> Path:
    """Resolve the markdown path for a session's summary."""
    return memory_class_dir(workspace, SESSION_SUMMARY_CLASS) / (
        f"{sanitize_session_key(session_key)}.md"
    )


def _headline_from(text: str) -> str:
    """First ~10 words of *text*, used as the entry headline."""
    words = text.strip().split()
    return " ".join(words[:10]) if words else "session summary"


def _parse_last_active(value: object) -> Optional[date]:
    """Best-effort date parse for the ``last_active`` ISO string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
    return None


def write_session_summary(
    workspace: Path,
    session_key: str,
    text: str,
    last_active: object = None,
) -> Optional[Path]:
    """Persist *text* as `memory/session_summary/<sanitized>.md`.

    Returns the path written, or ``None`` when *text* is empty
    (degenerate input is treated as "no summary" rather than
    written as a zero-byte entry).

    The entry id is the sanitised session key, so re-writing the
    summary for the same session overwrites the previous file —
    update semantics, not append.
    """
    text = (text or "").strip()
    if not text or text == "(nothing)":
        return None

    sanitized = sanitize_session_key(session_key)
    valid_from = _parse_last_active(last_active) or date.today()
    entry = MemoryEntry(
        id=sanitized,
        headline=_headline_from(text),
        summary=text,
        body=text,
        author="agent_created",
        valid_from=valid_from,
    )

    target = session_summary_path(workspace, session_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    save_entry(entry, target)
    return target


def get_session_summary(
    workspace: Path,
    session_key: str,
) -> Tuple[Optional[str], Optional[date]]:
    """Read the summary `.md` for *session_key*.

    Returns ``(text, last_active)`` or ``(None, None)`` when the
    file doesn't exist or doesn't parse. Best-effort — never raises.
    """
    path = session_summary_path(workspace, session_key)
    if not path.is_file():
        return (None, None)
    try:
        entry = load_entry(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "session_summary: failed to load %s: %s", path, exc,
        )
        return (None, None)
    return (entry.body or entry.summary or None, entry.valid_from)


def delete_session_summary(
    workspace: Path,
    session_key: str,
) -> bool:
    """Drop the `.md` for *session_key* if it exists.

    Used by `Session.clear()` so a reset wipes both the in-memory
    state and the on-disk projection. Returns True iff a file was
    actually deleted.
    """
    path = session_summary_path(workspace, session_key)
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as exc:
        logger.warning(
            "session_summary: failed to delete %s: %s", path, exc,
        )
        return False
    return True
