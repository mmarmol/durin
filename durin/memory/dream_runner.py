"""Async-safe runner for the entity-centric dream pipeline (doc 25 §2.A.1).

Wraps :class:`DreamConsolidator` with the production concerns that
the manual ``durin memory dream`` command could ignore but the
auto-triggers (cron daily, post-compaction, session-close, threshold
per-entity) cannot:

- **Lock file** at ``memory/.dream.lock`` so two triggers firing
  within the same window don't both consolidate (race → divergent
  pages, double git commits).
- **Throttle** with ``min_seconds_between_runs`` to absorb bursts —
  e.g. a flurry of ``memory_store`` calls each crossing the entity
  threshold should not produce a dream per call.
- **Stale-lock recovery**: a crashed previous run leaves a lock
  behind; treat lock files older than ``STALE_LOCK_SECONDS`` as
  abandoned and overwrite them. PID inside the lock helps diagnostics.
- **Telemetry**: ``memory.dream.start``, ``memory.dream.end``,
  ``memory.dream.skipped`` so the §2.E aggregator (durin memory stats)
  can show cost-per-day and trigger distribution.

The runner is **synchronous**. Callers that need non-blocking
behaviour wrap with ``asyncio.to_thread`` (cron callback) or
``threading.Thread`` (write-path hooks). Keeping the runner sync makes
testing simpler and the lifecycle (lock acquire → run → release) easy
to reason about in one place.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from durin.agent.tools._telemetry import emit_tool_event

logger = logging.getLogger(__name__)

__all__ = [
    "DreamRunResult",
    "DreamRunner",
]


# Lock older than this is treated as stale (crashed previous process).
# 10 min covers the longest realistic dream run (per Phase 0.3:
# ~36s/consolidation × 16 entities + retries ≈ ~10min worst case).
_STALE_LOCK_SECONDS = 600

# Throttle bookkeeping lives alongside the lock so a single mtime
# observation tells us "did we run recently?" without parsing JSON.
_LOCK_FILENAME = ".dream.lock"
_LAST_RUN_FILENAME = ".dream.last_run"


@dataclass(frozen=True)
class DreamRunResult:
    """Outcome of one dream pass.

    ``ran`` is True iff the consolidator actually executed (lock held,
    entries processed). False means we returned early — ``reason``
    explains why so callers and the §2.E telemetry aggregator can
    distinguish "no work to do" from "throttled" from "concurrent run".
    """

    ran: bool
    reason: str
    entities_consolidated: int
    entities_failed: int
    duration_s: float


@dataclass
class _ConsolidateTotals:
    """Mutable accumulator for one ``_consolidate`` invocation.

    A5: dream cost telemetry needs per-pass totals (sum across all
    entities) for `memory.dream.end`. The runner builds this as the
    iteration progresses, then folds the values into both the
    DreamRunResult (consolidated/failed for back-compat) and the
    telemetry payload (the four A5 fields).
    """

    consolidated: int = 0
    failed: int = 0
    quarantined: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0


class DreamRunner:
    """Coordinates one entity-centric dream pass with lock + throttle."""

    def __init__(
        self,
        workspace: Path,
        *,
        min_seconds_between_runs: int = 300,
        max_seconds_per_run: int = 600,
        model: str | None = None,
        vector_index: object | None = None,
        llm_invoke: Callable[..., str] | None = None,
        auto_absorb_enabled: bool = False,
        auto_absorb_threshold: int = 95,
        auto_absorb_min_age_hours: int = 24,
        auto_absorb_judge_model: str | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.memory_root = self.workspace / "memory"
        self.min_seconds_between_runs = max(0, int(min_seconds_between_runs))
        # max_seconds_per_run: hard cap on wall-clock time for one
        # dream pass (fix 2026-05-30 for data-loss bug). With FIFO
        # oldest-first batching + a re-discovery loop, the runner
        # drains an entity's backlog across multiple LLM calls within
        # one pass — this budget bounds how long that drain runs
        # before yielding to the next trigger. Per-pass not per-entity:
        # one greedy entity will consume the budget; remainder fires
        # `memory.dream.budget_exhausted` so operators see what got
        # deferred. Zero/negative = effectively "single batch per
        # entity then bail" (tests use 0; production default 600s).
        self.max_seconds_per_run = max(0, int(max_seconds_per_run))
        self.model = model
        self._vector_index = vector_index
        self._llm_invoke = llm_invoke
        # §2.D auto-absorb config. Default disabled — blast radius of a
        # silent false-positive merge is high enough that opt-in is the
        # right ergonomics. Threshold 95 favours precision; 24h
        # quarantine prevents the runner from judging pages it just
        # created (glm peer review C3, 2026-05-24).
        self._auto_absorb_enabled = bool(auto_absorb_enabled)
        self._auto_absorb_threshold = max(0, min(100, int(auto_absorb_threshold)))
        self._auto_absorb_min_age_hours = max(0, int(auto_absorb_min_age_hours))
        self._auto_absorb_judge_model = auto_absorb_judge_model

    # ------------------------------------------------------------------
    # public entry
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        trigger: str = "manual",
        entity_filter: str | None = None,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> DreamRunResult:
        """Execute one dream pass.

        ``trigger`` is a free-text label recorded on every telemetry
        event so the §2.E aggregator can split usage by source
        (``cron_daily`` / ``post_compaction`` / ``session_close`` /
        ``threshold`` / ``manual``).

        ``entity_filter`` narrows the pass to one entity ref
        (``person:marcelo``) — used by the threshold trigger.

        ``on_progress`` is an optional ``(entity_ref, status_msg)``
        callback for CLI display. Telemetry events fire regardless.
        """
        start = time.monotonic()

        # 0. Throttle check (skips the lock work when we're cooling
        #    down — cheap path for bursty triggers).
        if self._is_throttled():
            self._emit_skipped(trigger, "throttle", entity_filter)
            return DreamRunResult(
                ran=False, reason="throttle",
                entities_consolidated=0, entities_failed=0,
                duration_s=time.monotonic() - start,
            )

        # 1. Discover pending consolidations. If none, exit before
        #    acquiring the lock — no point serializing readers when
        #    there's nothing to write.
        pending = self._discover_pending(entity_filter)
        if not pending:
            self._emit_skipped(trigger, "no_pending", entity_filter)
            return DreamRunResult(
                ran=False, reason="no_pending",
                entities_consolidated=0, entities_failed=0,
                duration_s=time.monotonic() - start,
            )

        # 2. Acquire the lock. If another process beat us to it,
        #    return without complaint — the other run will handle
        #    these entities (or the next trigger will pick up the
        #    leftover).
        acquired = self._acquire_lock(trigger)
        if not acquired:
            self._emit_skipped(trigger, "concurrent_lock", entity_filter)
            return DreamRunResult(
                ran=False, reason="concurrent_lock",
                entities_consolidated=0, entities_failed=0,
                duration_s=time.monotonic() - start,
            )

        totals = _ConsolidateTotals()
        try:
            self._emit_start(trigger, entity_filter, len(pending))
            totals = self._consolidate(pending, on_progress, trigger=trigger)
        finally:
            self._release_lock()
            self._touch_last_run()

        duration = time.monotonic() - start
        self._emit_end(trigger, entity_filter, totals, duration)
        consolidated = totals.consolidated
        failed = totals.failed

        # §2.D: auto-absorb post-dream. Runs only when at least one
        # entity was consolidated (no point re-judging pairs that
        # haven't changed since the last pass) AND the feature is
        # explicitly enabled. Wrapped in try/except so a judge or
        # absorb failure NEVER masks a successful consolidate.
        if self._auto_absorb_enabled and consolidated > 0:
            try:
                self._maybe_auto_absorb()
            except Exception:
                logger.exception("auto_absorb pass raised; consolidate already done")

        return DreamRunResult(
            ran=True, reason="ok",
            entities_consolidated=consolidated,
            entities_failed=failed,
            duration_s=duration,
        )

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------

    def _discover_pending(self, entity_filter: str | None) -> dict[str, list[Any]]:
        """Reuse the CLI's discovery helper to keep the semantics
        identical between manual and auto-triggered runs."""
        from durin.cli.memory_cmd import _discover_pending_consolidations

        if not (self.memory_root / "episodic").exists():
            return {}
        return _discover_pending_consolidations(
            self.memory_root, entity_filter=entity_filter,
        )

    def _consolidate(
        self,
        pending: dict[str, list[Any]],
        on_progress: Callable[[str, str], None] | None,
        trigger: str = "manual",
    ) -> "_ConsolidateTotals":
        from durin.memory.dream import DreamConsolidator, DreamError

        kwargs: dict[str, Any] = {"workspace": self.workspace}
        if self.model is not None:
            kwargs["model"] = self.model
        if self._vector_index is not None:
            kwargs["vector_index"] = self._vector_index
        if self._llm_invoke is not None:
            kwargs["llm_invoke"] = self._llm_invoke
        consolidator = DreamConsolidator(**kwargs)

        # Wall-clock anchor for the per-pass time budget. A long-running
        # entity drain checks `time.monotonic() - started` against
        # `self.max_seconds_per_run` between batches and yields once
        # exhausted (next trigger picks up the rest — cursor has
        # advanced through whatever drained).
        started = time.monotonic()
        totals = _ConsolidateTotals()
        for ent_ref, entries in pending.items():
            try:
                drained = self._drain_entity(
                    consolidator, ent_ref, entries,
                    started=started, totals=totals,
                    on_progress=on_progress, trigger=trigger,
                )
                if drained:
                    totals.consolidated += 1
                if not drained and (time.monotonic() - started) >= self.max_seconds_per_run:
                    # Budget exhausted mid-entity → stop the outer loop
                    # too. Remaining entities are still pending; the
                    # next trigger will pick them up.
                    break
            except DreamError as exc:
                totals.failed += 1
                # A5: distinguish "failed and got quarantined" from
                # "failed but still has strikes left".
                if getattr(exc, "triggered_quarantine", False):
                    totals.quarantined += 1
                logger.warning("dream consolidate %s failed: %s", ent_ref, exc)
                if on_progress is not None:
                    on_progress(ent_ref, f"✗ {exc}")
            except Exception as exc:  # noqa: BLE001
                totals.failed += 1
                logger.exception("dream consolidate %s unexpected error", ent_ref)
                if on_progress is not None:
                    on_progress(ent_ref, f"✗ unexpected: {exc}")
        return totals

    def _drain_entity(
        self,
        consolidator: Any,
        ent_ref: str,
        entries: list[Any],
        *,
        started: float,
        totals: "_ConsolidateTotals",
        on_progress: Callable[[str, str], None] | None,
        trigger: str,
    ) -> bool:
        """Process batches of *entries* for *ent_ref* until drained or
        the per-pass budget is exhausted. Returns True iff fully drained.

        Each iteration:
          1. Consolidate one batch (consolidator's G11 cap takes the
             50 oldest), apply (cursor advances to batch_last_ts).
          2. Re-discover pending for this entity only — the cursor
             move filters the just-processed entries out, leaving
             whatever remained newer than batch_last_ts.
          3. If nothing left → drained, return True.
          4. If budget exhausted → emit `memory.dream.budget_exhausted`
             with the remaining count and return False so the caller
             stops touching this and any subsequent entity.
        """
        from durin.cli.memory_cmd import _discover_pending_consolidations

        remaining = list(entries)
        last_sha: str | None = None
        while remaining:
            result = consolidator.consolidate_entity(ent_ref, remaining)
            # A5: token usage accumulates across every batch of this drain.
            totals.prompt_tokens += int(getattr(result, "prompt_tokens", 0) or 0)
            totals.completion_tokens += int(getattr(result, "completion_tokens", 0) or 0)
            totals.llm_calls += int(getattr(result, "llm_call_count", 0) or 0)
            sha = consolidator.apply(ent_ref, result)
            if sha:
                last_sha = sha

            # Re-discover this entity's pending after the cursor advanced.
            refreshed = _discover_pending_consolidations(
                self.memory_root, entity_filter=ent_ref,
            )
            remaining = refreshed.get(ent_ref, [])
            if not remaining:
                if on_progress is not None:
                    msg = f"→ {last_sha[:8]}" if last_sha else "= no changes"
                    on_progress(ent_ref, msg)
                return True

            # Budget check — only AFTER a successful batch (so we always
            # make at least one batch of forward progress per entity).
            elapsed = time.monotonic() - started
            if elapsed >= self.max_seconds_per_run:
                emit_tool_event(
                    "memory.dream.budget_exhausted",
                    {
                        "trigger": trigger,
                        "entity_ref": ent_ref,
                        "pending_remaining": len(remaining),
                        "elapsed_s": round(elapsed, 3),
                        "budget_s": self.max_seconds_per_run,
                    },
                )
                if on_progress is not None:
                    on_progress(
                        ent_ref,
                        f"⏱ budget exhausted — {len(remaining)} entries deferred",
                    )
                return False
        return True

    # ------------------------------------------------------------------
    # lock + throttle
    # ------------------------------------------------------------------

    @property
    def _lock_path(self) -> Path:
        return self.memory_root / _LOCK_FILENAME

    @property
    def _last_run_path(self) -> Path:
        return self.memory_root / _LAST_RUN_FILENAME

    def _is_throttled(self) -> bool:
        """True when ``now - last_run < min_seconds_between_runs``."""
        if self.min_seconds_between_runs <= 0:
            return False
        try:
            mtime = self._last_run_path.stat().st_mtime
        except OSError:
            return False
        return (time.time() - mtime) < self.min_seconds_between_runs

    def _acquire_lock(self, trigger: str) -> bool:
        """Atomic O_CREAT|O_EXCL. Returns False if a fresh lock exists."""
        self.memory_root.mkdir(parents=True, exist_ok=True)
        # If a stale lock exists, remove it first. A stale lock is one
        # whose mtime is older than _STALE_LOCK_SECONDS — the previous
        # process either finished without releasing or crashed.
        try:
            stat = self._lock_path.stat()
        except OSError:
            stat = None
        if stat is not None and (time.time() - stat.st_mtime) > _STALE_LOCK_SECONDS:
            logger.warning("dream_runner: removing stale lock at %s", self._lock_path)
            try:
                self._lock_path.unlink()
            except OSError:
                pass

        try:
            fd = os.open(
                self._lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError:
            return False
        try:
            payload = json.dumps({
                "pid": os.getpid(),
                "started_at": time.time(),
                "trigger": trigger,
            })
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def _release_lock(self) -> None:
        try:
            self._lock_path.unlink()
        except OSError:
            pass

    def _touch_last_run(self) -> None:
        try:
            self._last_run_path.touch()
        except OSError as exc:
            logger.warning("dream_runner: failed to touch last_run marker: %s", exc)

    # ------------------------------------------------------------------
    # telemetry
    # ------------------------------------------------------------------

    def _emit_start(self, trigger: str, entity_filter: Optional[str], entities_pending: int) -> None:
        emit_tool_event(
            "memory.dream.start",
            {
                "trigger": trigger,
                "entity_filter": entity_filter or "",
                "entities_pending": entities_pending,
            },
        )

    def _emit_end(
        self,
        trigger: str,
        entity_filter: Optional[str],
        totals: _ConsolidateTotals,
        duration_s: float,
    ) -> None:
        # A5: payload now carries the per-pass token totals + the
        # quarantine counter + duration in ms (not s). Doc 07 §6.2
        # specifies this shape; doc 08 §3 R3 alarm
        # (`dream_llm_cost_per_day_usd > $5/day`) depends on the
        # `llm_input_tokens_total` + `llm_output_tokens_total` fields.
        emit_tool_event(
            "memory.dream.end",
            {
                "trigger": trigger,
                "entity_filter": entity_filter or "",
                "entities_consolidated": totals.consolidated,
                "entities_failed": totals.failed,
                "entities_quarantined": totals.quarantined,
                "llm_call_count": totals.llm_calls,
                "llm_input_tokens_total": totals.prompt_tokens,
                "llm_output_tokens_total": totals.completion_tokens,
                "duration_ms": duration_s * 1000.0,
            },
        )

    def _emit_skipped(self, trigger: str, reason: str, entity_filter: Optional[str]) -> None:
        emit_tool_event(
            "memory.dream.skipped",
            {
                "trigger": trigger,
                "reason": reason,
                "entity_filter": entity_filter or "",
            },
        )

    # ------------------------------------------------------------------
    # §2.D auto-absorb post-dream
    # ------------------------------------------------------------------

    def _maybe_auto_absorb(self) -> None:
        """Find alias-overlap candidates, judge, and auto-merge.

        Implements §2.D end-to-end:

        1. ``EntityAbsorption.find_candidates()`` — pairs sharing ≥1 alias.
        2. Cross-type filter — skip ``person:x`` vs ``project:x`` pairs.
        3. 24h quarantine — skip pairs where either page was created or
           last dreamed within ``min_age_hours`` (mitigates the
           premature-consolidation loop where the dream that just wrote
           two near-identical pages immediately judges them).
        4. LLM-judge via :func:`durin.memory.absorb_judge.judge_pair`.
        5. Merge only when ``verdict == "same"`` AND
           ``confidence >= threshold``.

        Telemetry covers every decision (judged / skipped / auto_merged)
        so §2.E aggregator can compute false-positive rate from the
        eventual ``memory.absorb.reverted`` signal.
        """
        from durin.memory.absorb_judge import JudgeError, judge_pair
        from durin.memory.absorption import EntityAbsorption

        absorber = EntityAbsorption(
            workspace=self.workspace, vector_index=self._vector_index,
        )
        candidates = absorber.find_candidates()
        if not candidates:
            return

        llm_invoke = self._llm_invoke_for_judge()
        judge_model = self._auto_absorb_judge_model or self.model or "glm-5.1"

        for cand in candidates:
            ref_a, ref_b = cand.refs
            type_a = ref_a.split(":", 1)[0] if ":" in ref_a else ""
            type_b = ref_b.split(":", 1)[0] if ":" in ref_b else ""

            # 2. Cross-type — different kinds of entity can legitimately
            # share a casual alias (admin / user). Drop without judging.
            if type_a != type_b:
                self._emit_absorb_skipped(ref_a, ref_b, 0, "cross_type")
                continue

            # Load pages with mtime for quarantine check + judge context.
            page_a, mtime_a = self._load_page_with_mtime(ref_a)
            page_b, mtime_b = self._load_page_with_mtime(ref_b)
            if page_a is None or page_b is None:
                self._emit_absorb_skipped(ref_a, ref_b, 0, "page_load_failed")
                continue

            # 3. User-authored protection (audit E19, doc 01 §4.6.1).
            # Auto-absorb must NOT touch entity pages the user wrote
            # by hand — they carry deliberate intent that the LLM
            # judge cannot recover. The check fires for either side
            # of the pair: a user-authored canonical and a user-
            # authored absorbed both protect the page from merging.
            if (
                page_a.author == "user_authored"
                or page_b.author == "user_authored"
            ):
                self._emit_absorb_skipped(
                    ref_a, ref_b, 0, "user_authored",
                )
                continue

            # 4. Quarantine — both pages must be older than the window.
            if self._is_quarantined(mtime_a, mtime_b):
                self._emit_absorb_skipped(ref_a, ref_b, 0, "quarantine")
                continue

            # 4. LLM-judge.
            judge_start = time.monotonic()
            try:
                judged = judge_pair(
                    canonical=page_a, absorbed=page_b,
                    shared_aliases=list(cand.shared_aliases),
                    llm_invoke=llm_invoke,
                    model=judge_model,
                    canonical_ref=ref_a, absorbed_ref=ref_b,
                    canonical_mtime=mtime_a, absorbed_mtime=mtime_b,
                )
            except JudgeError as exc:
                logger.warning(
                    "absorb_judge failed for %s vs %s: %s", ref_a, ref_b, exc,
                )
                self._emit_absorb_skipped(ref_a, ref_b, 0, "judge_failed")
                continue
            duration_ms = (time.monotonic() - judge_start) * 1000.0

            # Always emit judged for telemetry/tuning.
            self._emit_absorb_judged(ref_a, ref_b, judged, duration_ms)

            # 5. Decision.
            if judged.verdict != "same":
                self._emit_absorb_skipped(
                    ref_a, ref_b, judged.confidence,
                    f"verdict_{judged.verdict}",
                )
                continue
            if judged.confidence < self._auto_absorb_threshold:
                self._emit_absorb_skipped(
                    ref_a, ref_b, judged.confidence, "below_threshold",
                )
                continue

            # Pick canonical slug (D6) then absorb. Content from BOTH
            # pages is preserved via _merge_pages — the canonical slug
            # only decides which URL wins.
            canonical, absorbed = self._pick_canonical(
                ref_a, page_a, ref_b, page_b,
            )
            try:
                sha = absorber.absorb(
                    canonical, absorbed,
                    reason="auto",
                    judge_reasoning=judged.reasoning,
                    judge_confidence=judged.confidence,
                )
            except Exception:
                logger.exception(
                    "auto-absorb merge failed for %s ← %s", canonical, absorbed,
                )
                self._emit_absorb_skipped(
                    canonical, absorbed, judged.confidence, "absorb_failed",
                )
                continue
            self._emit_absorb_auto_merged(
                canonical, absorbed, judged.confidence, sha or "",
            )

    def _llm_invoke_for_judge(self) -> Callable[..., str]:
        """Resolve the LLM invoker for the judge call.

        Falls through to :func:`durin.memory.dream.default_llm_invoke`
        when the runner wasn't given an explicit one. Tests inject
        their own via ``llm_invoke=...`` in the constructor.
        """
        if self._llm_invoke is not None:
            return self._llm_invoke
        from durin.memory.dream import default_llm_invoke

        return default_llm_invoke

    def _load_page_with_mtime(
        self, ref: str,
    ) -> tuple[Optional[object], Optional[datetime]]:
        """Load an EntityPage + its file mtime. Returns (None, None) on miss.

        Type/slug split must succeed; archived pages live under a slug
        subfolder so we filter them out by checking that the resolved
        path is the top-level page (not a child of ``.../<slug>/...``).
        """
        from durin.memory.entity_page import EntityPage

        if ":" not in ref:
            return None, None
        type_, slug = ref.split(":", 1)
        path = self.memory_root / "entities" / type_ / f"{slug}.md"
        if not path.is_file():
            return None, None
        try:
            page = EntityPage.from_file(path)
        except Exception:  # noqa: BLE001
            return None, None
        if page is None:
            return None, None
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            mtime = None
        return page, mtime

    def _is_quarantined(
        self,
        mtime_a: Optional[datetime],
        mtime_b: Optional[datetime],
    ) -> bool:
        """True iff either page is younger than the quarantine window.

        ``min_age_hours <= 0`` disables the gate entirely. Missing
        mtimes are treated as too-new (cautious default) so a page
        that lost its file metadata doesn't accidentally bypass the
        quarantine.
        """
        if self._auto_absorb_min_age_hours <= 0:
            return False
        if mtime_a is None or mtime_b is None:
            return True
        threshold = datetime.now(timezone.utc) - timedelta(
            hours=self._auto_absorb_min_age_hours,
        )
        return mtime_a > threshold or mtime_b > threshold

    def _pick_canonical(
        self,
        ref_a: str, page_a: object,
        ref_b: str, page_b: object,
    ) -> tuple[str, str]:
        """Return (canonical_ref, absorbed_ref) per doc 25 §2.D D6.

        Selection ladder (first signal that breaks the tie wins):

        1. Page with newer ``dream_processed_through`` cursor —
           "more recently consolidated" proxies "more active".
        2. Page with more episodic entries referencing it — light
           centrality signal (more downstream weight).
        3. Alphabetically smaller ref — deterministic last resort,
           matches Hermes' tiebreaker pattern.

        Content from BOTH pages is preserved by
        :func:`EntityAbsorption._merge_pages`; this only decides which
        slug becomes the merged page's URL.
        """
        a_cursor = self._parse_cursor(getattr(page_a, "dream_processed_through", None))
        b_cursor = self._parse_cursor(getattr(page_b, "dream_processed_through", None))
        if a_cursor and b_cursor:
            if a_cursor > b_cursor:
                return ref_a, ref_b
            if b_cursor > a_cursor:
                return ref_b, ref_a
        elif a_cursor and not b_cursor:
            return ref_a, ref_b
        elif b_cursor and not a_cursor:
            return ref_b, ref_a

        a_refs = self._count_references(ref_a)
        b_refs = self._count_references(ref_b)
        if a_refs > b_refs:
            return ref_a, ref_b
        if b_refs > a_refs:
            return ref_b, ref_a

        return (ref_a, ref_b) if ref_a <= ref_b else (ref_b, ref_a)

    @staticmethod
    def _parse_cursor(value: object) -> Optional[datetime]:
        """Parse ``dream_processed_through`` to a UTC datetime or None.

        Numeric cursors (legacy ``msg_idx``) return None — not
        comparable to ISO timestamps; falls through to the next
        tiebreaker in :meth:`_pick_canonical`.
        """
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _count_references(self, ref: str) -> int:
        """Count episodic entries that reference this entity ref.

        Light centrality signal for :meth:`_pick_canonical`. O(N) per
        call where N is the episodic entry count — acceptable because
        this only fires when we're already about to commit a merge
        (rare, plus the dream pass just walked these entries anyway).
        """
        from durin.memory.paths import walk_class
        from durin.memory.storage import load_entry

        count = 0
        for path in walk_class(self.workspace, "episodic"):
            try:
                entry = load_entry(path)
            except Exception:  # noqa: BLE001
                continue
            if ref in entry.entities:
                count += 1
        return count

    def _emit_absorb_judged(
        self, canonical: str, absorbed: str, judged, duration_ms: float,
    ) -> None:
        emit_tool_event(
            "memory.absorb.judged",
            {
                "canonical": canonical,
                "absorbed": absorbed,
                "verdict": judged.verdict,
                "confidence": int(judged.confidence),
                "duration_ms": duration_ms,
            },
        )

    def _emit_absorb_auto_merged(
        self, canonical: str, absorbed: str, confidence: int, sha: str,
    ) -> None:
        emit_tool_event(
            "memory.absorb.auto_merged",
            {
                "canonical": canonical,
                "absorbed": absorbed,
                "confidence": int(confidence),
                "sha": sha,
            },
        )

    def _emit_absorb_skipped(
        self, canonical: str, absorbed: str, confidence: int, reason: str,
    ) -> None:
        emit_tool_event(
            "memory.absorb.skipped",
            {
                "canonical": canonical,
                "absorbed": absorbed,
                "confidence": int(confidence),
                "reason": reason,
            },
        )
