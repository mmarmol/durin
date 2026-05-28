"""Quarantine logic for entities that keep failing the Dream apply pipeline.

Per `docs/memory/05_dream_cold_path.md` §12.5:

- Structural failures (validation, patch_runtime, round_trip) on the
  same entity across consecutive passes increment
  ``dream_failure_count`` in the entity-page frontmatter.
- Ambient failures (LLM call, IO) are *not* counted — they signal
  infrastructure trouble, not entity trouble. Counting them would
  quarantine healthy entities during a z.ai outage.
- After 3 strikes, set ``dream_quarantine`` to now + 7 days. The
  runner skips entities with a future ``dream_quarantine`` value.
- A successful apply resets both fields.

The two new frontmatter keys live in ``page.extra`` (not first-class
on :class:`EntityPage`) because they're operational and the schema
spec deliberately avoids enshrining failure bookkeeping in the
canonical entity schema. Round-trip safety: ``EntityPage`` already
preserves unknown frontmatter under ``extra``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from durin.memory.dream_apply import DreamApplyFailureKind
from durin.memory.entity_page import EntityPage

__all__ = [
    "QUARANTINE_DURATION",
    "STRUCTURAL_FAILURE_KINDS",
    "clear_failures",
    "is_quarantined",
    "record_failure",
]


QUARANTINE_DURATION: timedelta = timedelta(days=7)

# Per §12.5 — only structural failures count toward the quarantine
# threshold. IO failures (disk full, permission errors, etc.) are
# ambient and treated like LLM call failures.
STRUCTURAL_FAILURE_KINDS: frozenset[DreamApplyFailureKind] = frozenset({
    DreamApplyFailureKind.VALIDATION,
    DreamApplyFailureKind.PATCH_RUNTIME,
    DreamApplyFailureKind.ROUND_TRIP,
})

_FAILURE_THRESHOLD: int = 3


def record_failure(
    page: EntityPage,
    kind: DreamApplyFailureKind,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Mutate *page* to reflect that an apply attempt failed.

    No-ops for ambient (non-structural) kinds. Sets
    ``dream_quarantine`` once the counter reaches
    :data:`_FAILURE_THRESHOLD`. The caller is responsible for
    persisting the page back to disk.

    Returns ``True`` iff this call was the one that crossed the
    quarantine threshold (audit A5 — used by the DreamRunner to
    increment its ``entities_quarantined`` counter for the
    ``memory.dream.end`` telemetry).
    """
    if kind not in STRUCTURAL_FAILURE_KINDS:
        return False
    extra = page.extra if isinstance(page.extra, dict) else {}
    page.extra = extra

    current = extra.get("dream_failure_count")
    if not isinstance(current, int) or current < 0:
        current = 0
    new_count = current + 1
    extra["dream_failure_count"] = new_count

    quarantine_triggered = (
        new_count >= _FAILURE_THRESHOLD
        and current < _FAILURE_THRESHOLD
    )
    if new_count >= _FAILURE_THRESHOLD:
        when = (now or datetime.now(timezone.utc)) + QUARANTINE_DURATION
        extra["dream_quarantine"] = when.isoformat()
    return quarantine_triggered


def clear_failures(page: EntityPage) -> None:
    """Drop the quarantine bookkeeping fields. Called on successful apply."""
    extra = page.extra if isinstance(page.extra, dict) else {}
    extra.pop("dream_failure_count", None)
    extra.pop("dream_quarantine", None)
    page.extra = extra


def is_quarantined(
    page: EntityPage,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """True iff *page* carries a ``dream_quarantine`` in the future.

    Malformed timestamps are treated as "not quarantined" — failing
    open here keeps a busted frontmatter value from permanently
    blocking an entity. The next failure overwrites the bad value
    with a fresh one.
    """
    extra = page.extra if isinstance(page.extra, dict) else {}
    raw = extra.get("dream_quarantine")
    if not isinstance(raw, str):
        return False
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return when > current
