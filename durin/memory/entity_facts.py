"""What the per-build walks read from every entity page, parsed once per
version of the page.

Every live render of the prompt's memory surface walks all entity pages: the
pinned block lists the ``always_on`` pages, and the hot layer orders pages by
when they were last updated. Parsing every page's YAML on each walk took
seconds per walk on a workspace of a few thousand pages, and it ran on the
event loop, stalling the gateway while a new session built its first prompt.
The walks need three facts per page, and those change only when the page's
text does, so they are kept for the life of the process, keyed by the text: a
walk still reads every file, which is cheap, and parses only the pages whose
text changed since the last one.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from durin.memory.entity_page import EntityPage


@dataclass(frozen=True)
class PageFacts:
    """What the walks read from a page."""

    type: str
    always_on: bool
    # The page's ``updated_at`` as an ISO string (the field, or the raw
    # string an older page carries), or None when it has neither — the
    # caller then falls back to the file's mtime, which the text does not
    # carry.
    updated_at: str | None


_lock = threading.Lock()
_known: dict[str, tuple[tuple[int, int], PageFacts | None]] = {}


def page_facts(path: Path) -> PageFacts | None:
    """The facts of the page at ``path``, or None when it is not a valid
    page. A read error propagates, as from ``EntityPage.from_file``."""
    text = Path(path).read_text(encoding="utf-8")
    fingerprint = (len(text), hash(text))
    key = str(path)
    with _lock:
        known = _known.get(key)
    if known is not None and known[0] == fingerprint:
        return known[1]
    facts = _facts_of(EntityPage.from_text(text))
    with _lock:
        _known[key] = (fingerprint, facts)
    return facts


def _facts_of(page: EntityPage | None) -> PageFacts | None:
    if page is None:
        return None
    if page.updated_at is not None:
        updated: str | None = page.updated_at.isoformat()
    else:
        raw = page.extra.get("updated_at", "") if page.extra else ""
        updated = raw if isinstance(raw, str) and raw else None
    return PageFacts(
        type=page.type,
        always_on=bool(page.attributes.get("always_on")),
        updated_at=updated,
    )
