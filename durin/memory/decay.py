"""Temporal decay configuration.

Per `docs/memory/03_search_pipeline.md` §10:

- Each memory class has a default half-life in days. Observation-type
  classes (`episodic`, `session_summary`) decay; canonical-state
  classes (`entity`, `stable`, `corpus`) do not (their mtime is "last
  Dream update", not "fact age", so decaying them would punish
  freshly-consolidated material).
- Per-entry override: a `decay_half_life: <int|null>` frontmatter
  field overrides the class default. A `null` value is a meaningful
  signal — "this entry is a permanent fact, never decay it".
- `evergreen: true` wins over everything.

Phase 0 scope: the half-life table + the `half_life_for` resolver. The
ranking-time consumer (apply exponential decay to score) lands in a
later phase.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from durin.memory.schema import MemoryEntry

__all__ = ["CLASS_HALF_LIFE_DEFAULTS", "half_life_for"]


# Days, per doc memory §10.2. `None` = never decay.
CLASS_HALF_LIFE_DEFAULTS: dict[str, Optional[int]] = {
    "episodic": 90,
    "session_summary": 120,
    "entity": None,
    "stable": None,
    "corpus": None,
}


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
