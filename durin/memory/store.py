"""Write memory entries to ``memory/<class>/<id>.md``.

The agent (or any caller) hands a
piece of distilled content; this module gives it a stable id, fills
the multi-resolution frontmatter (auto-generating the headline if not
supplied), tags the entry with the ambient provenance author, and
writes it to the canonical per-class path.

The id is a deterministic 12-char hash of ``(class, content)`` so the
same content stored twice under the same class is a no-op rewrite.
Different classes get different ids — same fact in ``episodic`` and
``stable`` is allowed and intentional.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

from durin.memory.paths import MEMORY_CLASSES, memory_class_dir
from durin.memory.provenance import current_author
from durin.memory.schema import MemoryEntry
from durin.memory.storage import save_entry

__all__ = ["StoreError", "store_memory"]


class StoreError(ValueError):
    """Raised when a memory entry cannot be stored."""


def store_memory(
    workspace: Path,
    *,
    content: str,
    class_name: str = "episodic",
    headline: str | None = None,
    summary: str = "",
    source_refs: list[str] | None = None,
    related: list[str] | None = None,
    entities: list[str] | None = None,
    valid_from: date | None = None,
) -> dict[str, Any]:
    """Persist a memory entry; returns metadata about what was written.

    ``content`` is the markdown body (required, non-empty). ``headline``
    defaults to the first ~10 words of the content. ``author`` is taken
    from the ambient :func:`~durin.memory.provenance.current_author`
    ContextVar so the curator and dream can later distinguish
    ``agent_created`` from ``user_authored`` entries without the caller
    having to thread it through explicitly.
    """
    if not content or not content.strip():
        raise StoreError("content is required")
    if class_name not in MEMORY_CLASSES:
        raise StoreError(
            f"unknown class {class_name!r}; expected one of {MEMORY_CLASSES}"
        )

    # Strict entity validation on the write path: every entity ref must
    # match the <type>:<value> shape. The vocabulary of types is open;
    # only the form is checked.
    entities_list = list(entities or [])
    if entities_list:
        from durin.memory.entities import is_valid_entity_ref

        bad = [e for e in entities_list if not is_valid_entity_ref(e)]
        if bad:
            raise StoreError(
                f"invalid entity reference(s): {bad}. "
                f"Format: '<type>:<value>' where type is lowercase "
                f"[a-z][a-z0-9_]* (e.g. person:marcelo, project:durin)."
            )

    if headline is None:
        headline = _auto_headline(content)

    entry_id = _content_id(class_name, content)
    entry = MemoryEntry(
        id=entry_id,
        headline=headline,
        summary=summary,
        source_refs=list(source_refs or []),
        related=list(related or []),
        entities=entities_list,
        author=current_author(),
        valid_from=valid_from or date.today(),
        body=content,
    )

    target = memory_class_dir(workspace, class_name) / f"{entry_id}.md"
    save_entry(entry, target)

    return {
        "id": entry_id,
        "class": class_name,
        "path": str(target),
        "headline": headline,
        "author": entry.author,
    }


def _auto_headline(content: str) -> str:
    """Pull the first ~10 words from the content as a headline fallback."""
    words = content.strip().split()
    return " ".join(words[:10]) if words else "memory entry"


def _content_id(class_name: str, content: str) -> str:
    """Deterministic 12-char id from (class, content). Idempotent re-store."""
    h = hashlib.sha256()
    h.update(class_name.encode("utf-8"))
    h.update(b"\0")
    h.update(content.encode("utf-8"))
    return h.hexdigest()[:12]
