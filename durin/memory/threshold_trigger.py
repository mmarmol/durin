"""Per-entity threshold trigger for entity-centric Dream consolidation.

Shared helper used by both ``memory_store`` and ``memory_ingest`` tools.
The pattern (doc 25 §2.A.1 β.2, originally lived in ``memory_store`` only):

  1. After a successful write that tagged at least one entity,
  2. Count post-cursor entries per entity across episodic + corpus
     (corpus counts as a SIGNAL of "user active on this entity"
     even though Dream itself only consolidates episodic).
  3. When any entity crosses ``threshold_entries``, spawn a daemon
     thread that invokes :class:`DreamRunner` with a tag that
     identifies which write surface triggered (``trigger="threshold"``
     for store-path, ``trigger="post_ingest_threshold"`` for ingest).

Burst protection is 100% delegated to ``DreamRunner``:
- File lock at ``memory/.dream.lock`` (rejects concurrent runs).
- Throttle via ``min_seconds_between_runs`` (rejects rapid re-fires).
- Stale-lock recovery for crashed runs.

We deliberately do **not** add a process-local dedup window or any
mutable global state — earlier draft did and a code review caught race
conditions + a memory leak. The few extra threads that spawn and
immediately die against the lock are cheap; the missing dedup is
unobservable.

Telemetry trigger labels:
- ``"threshold"`` — original store-path, kept for backward compat with
  existing dashboards (do not rename).
- ``"post_ingest_threshold"`` — ingest-path, new.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover
    from durin.memory.vector_index import VectorIndex

logger = logging.getLogger(__name__)

__all__ = ["count_pending_for_trigger", "maybe_dispatch_threshold_dream"]


def count_pending_for_trigger(
    workspace: Path,
    *,
    entity_filter: str | None = None,
) -> dict[str, int]:
    """Count per-entity write activity that should signal "consolidate me".

    Sums two contributions, both filtered by ``entity_filter`` when set:

    1. **Episodic post-cursor entries** — entries newer than the entity
       page's ``dream_processed_through`` cursor. These are the ones
       Dream will actually consolidate; the count is sourced via the
       same helper Dream itself uses (so we don't double-count).
    2. **Corpus entries** tagged with the entity — Dream does NOT
       consolidate corpus (an ingested doc is already canonical-ish on
       its own), but if the user has been actively dropping docs about
       an entity, that's a signal the entity is hot and worth
       consolidating its episodic backlog.

    Returns ``{entity_ref: count}`` for entities that have at least
    one entry. Missing keys mean "zero".
    """
    counts: dict[str, int] = {}

    # 1) Episodic post-cursor (reuse Dream's discovery helper to keep
    # the cursor semantics identical to what Dream itself sees).
    try:
        from durin.cli.memory_cmd import _discover_pending_consolidations

        memory_root = workspace / "memory"
        pending = _discover_pending_consolidations(
            memory_root, entity_filter=entity_filter,
        )
        for ref, entries in pending.items():
            counts[ref] = counts.get(ref, 0) + len(entries)
    except Exception as exc:  # noqa: BLE001
        logger.warning("threshold trigger: discover episodic failed: %s", exc)

    # 2) Corpus entries tagged with the entity. Cursor doesn't apply
    # to corpus (no consolidation pass over it). Best-effort walk; a
    # malformed file is skipped, not propagated.
    try:
        from durin.memory.paths import walk_class
        from durin.memory.storage import load_entry

        for path in walk_class(workspace, "corpus"):
            try:
                entry = load_entry(path)
            except Exception:
                continue
            for ref in entry.entities or ():
                if entity_filter is not None and ref != entity_filter:
                    continue
                counts[ref] = counts.get(ref, 0) + 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("threshold trigger: walk corpus failed: %s", exc)

    return counts


def maybe_dispatch_threshold_dream(
    *,
    workspace: Path,
    entities: list[str],
    dream_config: Any | None,
    vector_index: Optional["VectorIndex"],
    source_trigger: str,
    app_config: Any | None = None,
) -> None:
    """Per-entity threshold check + background dispatch.

    Called after a successful write (store or ingest). For each entity
    in the write, sums the per-entity signal (episodic post-cursor +
    corpus). When any entity is at-or-above ``threshold_entries``,
    spawn a daemon thread that invokes :meth:`DreamRunner.run` with the
    given ``source_trigger`` label.

    The dispatch is fire-and-forget — must NOT block the caller.
    Failures are logged, never propagated. ``DreamRunner``'s lock +
    throttle handle concurrent dispatches: 10 threads spawned in a
    burst will see the lock and emit ``memory.dream.skipped``; one
    actually runs.

    Parameters
    ----------
    workspace
        Workspace path containing ``memory/`` and the dream lock.
    entities
        Entity refs from the just-written entry (e.g. ``["person:alice"]``).
        Empty list short-circuits to no-op.
    dream_config
        ``MemoryDreamConfig`` instance (or ``None`` to disable). Read
        ``enabled``, ``threshold_entries``, ``min_seconds_between_runs``,
        ``model_override``, ``auto_absorb.*``.
    vector_index
        Optional ``VectorIndex`` passed to ``DreamRunner`` so dream's
        per-entity upserts go through it. ``None`` is fine.
    source_trigger
        Telemetry label written into ``memory.dream.start.trigger``.
        Use ``"threshold"`` for the legacy store-path (backward compat),
        ``"post_ingest_threshold"`` for the new ingest-path.
    app_config
        Optional full ``DurinConfig``. When provided, the dream's model
        is resolved via :func:`durin.memory.model_resolve.resolve_memory_model`
        (which honours ``aux_models.memory``). When ``None``, the legacy
        ``dream_config.model_override`` is used directly.
    """
    if not entities:
        return
    if dream_config is None or not getattr(dream_config, "enabled", False):
        return
    threshold = int(getattr(dream_config, "threshold_entries", 0) or 0)
    if threshold <= 0:
        return

    counts = count_pending_for_trigger(workspace)
    triggered_for = [
        ref for ref in entities
        if counts.get(ref, 0) >= threshold
    ]
    if not triggered_for:
        return

    import threading

    from durin.memory.dream_runner import DreamRunner
    from durin.memory.model_resolve import resolve_memory_model

    auto_cfg = getattr(dream_config, "auto_absorb", None)
    if app_config is not None:
        resolved_model = resolve_memory_model(app_config)
    else:
        resolved_model = getattr(dream_config, "model_override", None)
    for ref in triggered_for:
        def _run(entity_ref: str = ref) -> None:
            try:
                runner = DreamRunner(
                    workspace=workspace,
                    min_seconds_between_runs=int(
                        getattr(dream_config, "min_seconds_between_runs", 300),
                    ),
                    max_seconds_per_run=int(
                        getattr(dream_config, "max_seconds_per_run", 600),
                    ),
                    model=resolved_model,
                    vector_index=vector_index,
                    auto_absorb_enabled=bool(
                        getattr(auto_cfg, "enabled", False),
                    ),
                    auto_absorb_threshold=int(
                        getattr(auto_cfg, "confidence_threshold", 95),
                    ),
                    auto_absorb_min_age_hours=int(
                        getattr(auto_cfg, "min_age_hours", 24),
                    ),
                    auto_absorb_judge_model=getattr(
                        auto_cfg, "judge_model", None,
                    ),
                )
                runner.run(trigger=source_trigger, entity_filter=entity_ref)
            except Exception:
                logger.exception(
                    "%s dream for %s failed", source_trigger, entity_ref,
                )

        threading.Thread(
            target=_run, daemon=True,
            name=f"dream-{source_trigger}-{ref}",
        ).start()
