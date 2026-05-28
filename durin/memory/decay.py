"""Temporal decay configuration and ranking-time consumer.

Per `docs/memory/03_search_pipeline.md` §10 and audit A9
(2026-05-28):

- Each memory class has a default half-life in days. Observation-type
  classes (`episodic`, `session_summary`) decay; canonical-state
  classes (`entity` / `entity_page`, `stable`, `corpus`) do not (their
  mtime / valid_from doesn't represent "fact age" — see the reasoning
  table below).
- Per-entry override (frontmatter `decay_half_life` / `evergreen`)
  exists in :class:`durin.memory.schema.MemoryEntry` but **is NOT
  applied in the search pipeline** as of A9. The override is honoured
  in paths that read the full `MemoryEntry` (hot_layer, dream). The
  LanceDB row doesn't carry these fields; promoting them would
  require a schema bump and a forced rebuild — deferred until a real
  use case appears (no producer sets them today; Dream's prompt
  doesn't instruct the LLM to emit them either).

A9 added :func:`apply_class_decay` — the ranking-time consumer the
Phase 0 header foreshadowed. It uses class defaults only, per the
decision logged in doc 11.

Reasoning per class (verified honestly during A9, not copied from
the original spec table):

- `entity_page`: no decay. `valid_from` is empty for entity pages,
  and the file mtime tracks "last Dream pass", not "age of facts on
  the page".
- `episodic`: 90-day half-life. Observations naturally age. Recent
  observations (≪ 90d) barely register decay; very old ones
  (5× half-life ≈ 450d) round to ~0.7% of original score but can
  still surface if strongly relevant.
- `stable`: no decay. The user/agent explicitly marked these as
  durable; decaying contradicts that decision.
- `corpus`: no decay. `valid_from` is the INGEST date, not the
  content date. Decaying corpus would penalise "old books in your
  pipeline", not obsolete information.
- `session_summary`: 120-day half-life. Session digests age, but
  more slowly than episodic (they cover broader topics). Inert
  until the session-summary emitter ships (audit A10).
- `pending`: excluded from the walker (A2); never reaches the
  search pipeline.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from durin.memory.schema import MemoryEntry

logger = logging.getLogger(__name__)

__all__ = [
    "CLASS_HALF_LIFE_DEFAULTS",
    "apply_class_decay",
    "half_life_for",
    "resolve_class_half_life",
]


# Days, per doc memory §10.2. `None` = never decay.
# `entity_page` is an alias for `entity` (LanceDB uses the longer
# name); both map to the same null half-life.
CLASS_HALF_LIFE_DEFAULTS: dict[str, Optional[int]] = {
    "episodic": 90,
    "session_summary": 120,
    "entity": None,
    "entity_page": None,
    "stable": None,
    "corpus": None,
}


def resolve_class_half_life(
    class_name: str,
    *,
    overrides: Optional[dict[str, Optional[int]]] = None,
) -> Optional[int]:
    """Lookup the class half-life in days; ``None`` = no decay.

    Unknown classes return ``None`` unless an override is supplied —
    safe default: an entry whose class we don't recognise passes
    through unchanged rather than getting an arbitrary half-life
    applied.

    Audit F1 (2026-05-28): accepts the operator-configured
    ``overrides`` map (`memory.search.temporal_decay
    .class_half_life_overrides`). Override semantics:

    - Present + integer → use that half-life (in days).
    - Present + ``None`` → disable decay for the class even when the
      default would decay it.
    - Absent → fall through to the per-class default.

    Same lookup is used by both `apply_class_decay` (the ranking-time
    consumer) and `half_life_for` (kept for legacy callers).
    """
    if overrides is not None and class_name in overrides:
        return overrides[class_name]
    return CLASS_HALF_LIFE_DEFAULTS.get(class_name)


def apply_class_decay(
    *,
    score: float,
    class_name: str,
    valid_from_iso: str,
    now: Optional[datetime] = None,
    overrides: Optional[dict[str, Optional[int]]] = None,
) -> tuple[float, float]:
    """Multiply *score* by exp(-Δdays / half_life), per class half-life.

    Audit A9 (2026-05-28). The ranking-time consumer of the half-life
    table. Per-class only — per-entry overrides are NOT applied here
    (see module docstring).

    Audit F1 (2026-05-28): forwards ``overrides`` to
    ``resolve_class_half_life`` so operator config tuning (`memory
    .search.temporal_decay.class_half_life_overrides`) flows through
    this hot path.

    Returns ``(new_score, decay_factor)``:

    - ``decay_factor == 1.0`` means "no decay applied" — either the
      class doesn't decay, the timestamp is missing/malformed, or
      Δdays is non-positive (future-dated entries pass through).
    - ``decay_factor`` in ``(0, 1)`` is the multiplier applied.

    The caller is expected to swallow failures via the returned
    ``decay_factor`` — this function never raises.
    """
    half_life = resolve_class_half_life(class_name, overrides=overrides)
    if half_life is None or half_life <= 0:
        return score, 1.0
    if not valid_from_iso:
        return score, 1.0

    parsed = _parse_iso_date(valid_from_iso)
    if parsed is None:
        return score, 1.0

    now_dt = now or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta_days = (now_dt - parsed).total_seconds() / 86_400.0
    # Future-dated entries (clock skew, optimistic timestamps) keep
    # their score — punishing them with a positive multiplier > 1.0
    # would be the wrong semantics (decay only acts in the past).
    if delta_days <= 0:
        return score, 1.0

    factor = math.exp(-delta_days / half_life)
    return score * factor, factor


def _parse_iso_date(value: str) -> Optional[datetime]:
    """Best-effort ISO parser for `valid_from` strings.

    Handles ``YYYY-MM-DD`` (the common shape from `store_memory`'s
    ``date.today()`` default) and full RFC 3339 timestamps. Returns
    ``None`` on any parse failure so the caller can fall through to
    "no decay" semantics.
    """
    s = value.strip()
    if not s:
        return None
    # `datetime.fromisoformat` handles `YYYY-MM-DD` and the common
    # `YYYY-MM-DDTHH:MM:SS[+TZ]` shapes in Python 3.11+.
    try:
        if "T" in s or " " in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s + "T00:00:00+00:00")
    except (ValueError, TypeError):
        return None


def half_life_for(
    entry: "MemoryEntry",
    *,
    class_name: str,
    decay_field_set: bool = False,
) -> Optional[int]:
    """Resolve the effective half-life (days) for a ranking-time hit.

    Logic (doc memory §10.5)::

        if entry.evergreen:
            return None
        if entry has explicit decay_half_life (set by user/dream):
            return that value          # may itself be None
        return class default            # may itself be None

    Parameters
    ----------
    entry
        The :class:`MemoryEntry` whose half-life is being computed.
    class_name
        Memory class string (``"episodic"``, ``"entity"``, …).
        Unknown class → ``None`` (safe: no decay).
    decay_field_set
        ``True`` when the caller has determined that the entry's
        frontmatter explicitly carried a ``decay_half_life`` key. The
        loader uses ``model_fields_set`` for this; we don't read it
        here because constructed-in-memory entries set the field but
        with the default of ``None``, which is indistinguishable from
        unset without the caller's help.
    """
    if entry.evergreen:
        return None
    if decay_field_set:
        return entry.decay_half_life
    return CLASS_HALF_LIFE_DEFAULTS.get(class_name)
