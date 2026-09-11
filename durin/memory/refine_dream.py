"""Refine dream (periodic) — graph hygiene: dedup duplicate entities.

Reuses the existing absorb machinery (``EntityAbsorption.find_candidates`` +
``absorb`` + ``absorb_judge.judge_pair``). In the new model absorb is ON by
default but CONSERVATIVE (confidence threshold 95). It RESPECTS:
- **do_not_absorb tombstones** — a pair the user rejected/un-merged is never
  re-merged;
- **user-managed pages** — a page the user opted to manage (page-level
  ``author == user_authored``) is left alone.

Recovery of a bad merge is ``git revert`` of the absorb commit; recording the
tombstone afterward (``add_tombstone``) stops the next refine from undoing the
user's revert.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from durin.memory.absorb_judge import (
    JudgeError,
    judge_pair,
    judge_template_fingerprint,
)
from durin.memory.absorption import EntityAbsorption
from durin.memory.entity_page import EntityPage
from durin.memory.llm_invoke import default_llm_invoke
from durin.utils.atomic_write import atomic_write_text

__all__ = ["is_tombstoned", "add_tombstone", "add_flagged", "read_flagged", "remove_flagged", "run_refine"]

LLMInvoke = Callable[..., Any]
_TOMBSTONE_FILE = ".refine_tombstones.json"
_FLAGGED_FILE = ".flagged_pairs.json"
# Cost bound: a single refine run never fans out more than this many Tier-2
# sub-agent investigations. Past it, borderline pairs keep the cheap verdict,
# emit a `memory.absorb.escalation_capped` event, and are flagged for manual
# review in the Bandeja (never silent).
_MAX_ESCALATIONS_PER_RUN = 25


def _emit(event: str, **data: Any) -> None:
    """Best-effort dream telemetry (reuses the legacy memory.absorb.* names)."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(event, data)
    except Exception:  # pragma: no cover — telemetry must never break the dream
        pass


def _created_this_run(page: "EntityPage", run_started_at: Any) -> bool:
    """True when the entity was created at/after the run began — the run looking
    at its own fresh output. created_at falls back to updated_at; with no
    timestamp the entity is treated as established (fail open, don't block)."""
    ts = page.created_at or page.updated_at
    if ts is None or run_started_at is None:
        return False
    return ts >= run_started_at


def _tombstone_path(workspace: Path) -> Path:
    return Path(workspace) / "memory" / _TOMBSTONE_FILE


# --- verdict cache -----------------------------------------------------------
#
# A standing candidate pair whose members haven't changed re-emerges every run
# (alias overlap and embedding distance are deterministic), and "different"
# changes nothing on disk — so without memory of the verdict the judge re-answers
# the same question nightly (observed live: 83% of one run's judgments repeated
# the previous run's, ~3h of aux-LLM calls). Settled "different" verdicts are
# memoized here, keyed by the judgment-bearing content of both pages plus the
# judge identity (template + model). Only plain Tier-1 "different" is cached:
# merged pairs vanish on their own, and borderline outcomes (unclear /
# below-threshold same) must stay re-examinable.

_VERDICTS_FILE = ".refine_verdicts.json"


def _verdicts_path(workspace: Path) -> Path:
    return Path(workspace) / "memory" / _VERDICTS_FILE


def _judge_content_fingerprint(page: "EntityPage") -> str:
    """Hash of the fields a verdict can turn on: identity and content.

    Deliberately EXCLUDES provenance, derived_from and timestamps — source
    accrual (every seeding pass adds one) must not reopen a settled pair —
    and the always_on attribute: it is curation state the always_on pass
    re-ranks after every run, not identity content."""
    import hashlib

    payload = json.dumps({
        "type": page.type,
        "name": page.name,
        "aliases": sorted(page.aliases or []),
        "attributes": {
            k: v for k, v in (page.attributes or {}).items() if k != "always_on"},
        "relations": sorted(
            f"{r.get('to')}|{r.get('type')}" for r in (page.relations or [])),
        "body": page.body or "",
    }, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_now = time.time  # module-level so tests can move the wall clock
_clock = time.perf_counter  # module-level so tests can move the budget clock

# Provider failures in a row before the pass stops for this run: an expired
# key or an outage is a property of the moment, not of any pair, and every
# further call would only fail the same way.
_MAX_CONSECUTIVE_PROVIDER_FAILURES = 3

# Verdict-cache flush policy: at most this many judged pairs, or this many
# seconds, between saves — an interrupted run loses at most one window.
_CACHE_FLUSH_EVERY = 20
_CACHE_FLUSH_SECONDS = 60.0


def _load_tombstones(workspace: Path) -> set[str]:
    p = _tombstone_path(workspace)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _pair_fingerprint(
    ref_a: str, page_a: "EntityPage", ref_b: str, page_b: "EntityPage",
) -> str:
    """Order-stable combined fingerprint (follows the sorted pair key)."""
    items = sorted([(ref_a, page_a), (ref_b, page_b)], key=lambda i: i[0])
    return ":".join(_judge_content_fingerprint(p) for _, p in items)


def _load_verdict_cache(workspace: Path) -> dict[str, dict]:
    p = _verdicts_path(workspace)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_verdict_cache(workspace: Path, cache: dict[str, dict]) -> None:
    p = _verdicts_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(cache, sort_keys=True, ensure_ascii=False))


def _pair_key(a: str, b: str) -> str:
    return "|".join(sorted([a, b]))


def is_tombstoned(workspace: Path, ref_a: str, ref_b: str) -> bool:
    p = _tombstone_path(workspace)
    if not p.exists():
        return False
    try:
        keys = set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return False
    return _pair_key(ref_a, ref_b) in keys


def add_tombstone(workspace: Path, ref_a: str, ref_b: str) -> None:
    """Record that the user rejected merging this pair — refine never re-merges."""
    p = _tombstone_path(workspace)
    keys: set[str] = set()
    if p.exists():
        try:
            keys = set(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            keys = set()
    keys.add(_pair_key(ref_a, ref_b))
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(sorted(keys)))


def _flagged_path(workspace: Path) -> Path:
    return Path(workspace) / "memory" / _FLAGGED_FILE


def add_flagged(
    workspace: Path,
    ref_a: str,
    ref_b: str,
    *,
    verdict: str,
    confidence: int,
    reasoning: str,
) -> None:
    """Record a pair the Tier-2 agent investigated but did not confirm as same,
    or a borderline pair capped before Tier-2 ran (escalation budget exhausted
    for the run) and kept on its cheap Tier-1 verdict instead.

    The record is keyed by sorted pair so order does not matter. A duplicate
    pair key keeps the newest record. Write failures are swallowed so a store
    error never breaks the refine pass.
    """
    from datetime import datetime, timezone
    p = _flagged_path(workspace)
    records: dict[str, dict] = {}
    if p.exists():
        try:
            for rec in json.loads(p.read_text(encoding="utf-8")):
                key = _pair_key(*rec["pair"])
                records[key] = rec
        except Exception:
            records = {}
    key = _pair_key(ref_a, ref_b)
    records[key] = {
        "pair": sorted([ref_a, ref_b]),
        "verdict": verdict,
        "confidence": confidence,
        "reasoning": reasoning,
        "at": datetime.now(tz=timezone.utc).isoformat(),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(list(records.values()), indent=2))
    except Exception:  # pragma: no cover — write failure must not break refine
        pass
    _emit("memory.dream.flagged", canonical=ref_a, absorbed=ref_b)


def read_flagged(workspace: Path) -> list[dict]:
    """Return all flagged pairs from the store, newest-wins per key."""
    p = _flagged_path(workspace)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def remove_flagged(workspace: Path, ref_a: str, ref_b: str) -> None:
    """Drop the entry for the given pair from the flagged-pairs store.

    Keyed by the sorted pair so argument order does not matter.  No-ops when
    the pair is not present.  Write failures are swallowed (best-effort) so a
    store error never breaks the caller.
    """
    p = _flagged_path(workspace)
    if not p.exists():
        return
    try:
        records: dict[str, dict] = {}
        for rec in json.loads(p.read_text(encoding="utf-8")):
            records[_pair_key(*rec["pair"])] = rec
    except Exception:
        return
    target = _pair_key(ref_a, ref_b)
    if target not in records:
        return
    del records[target]
    try:
        atomic_write_text(p, json.dumps(list(records.values()), indent=2))
    except Exception:  # pragma: no cover — write failure must not break caller
        pass


def _load_page(workspace: Path, ref: str) -> EntityPage | None:
    type_, _, slug = ref.partition(":")
    path = Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md"
    try:
        return EntityPage.from_file(path) if path.exists() else None
    except OSError:
        # A live memory commit can reset the working tree between the
        # existence check and the read; the pair is re-examined next run.
        return None


def _page_mtime(workspace: Path, ref: str):
    """File mtime of an entity page as a UTC datetime (N7a) — fed to the absorb
    judge so it can reason about staleness ("observed years apart"). None when
    the file is unreadable."""
    from datetime import datetime, timezone
    type_, _, slug = ref.partition(":")
    path = Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md"
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _escalate_judge(workspace: Path, ref_a: str, ref_b: str, **kw: object) -> "JudgeResult":
    """Thin wrapper so tests can monkeypatch without importing tier2_judge at module load."""
    from durin.memory.tier2_judge import escalate_judge
    return escalate_judge(workspace, ref_a, ref_b, **kw)


def run_refine(
    workspace: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    confidence_threshold: int = 95,
    escalate_floor: int = 0,
    run_started_at: "datetime | None" = None,
    vector_index: object | None = None,
    semantic_distance_threshold: float = 0.30,
    max_seconds: float = 0,
    judge_concurrency: int = 1,
    recheck_cooldown_s: float = 7 * 86400,
    semantic_name_gate: str = "prioritize",
) -> dict:
    """Dedup pass: judge alias-overlap candidate pairs and merge the same ones.

    ``run_started_at`` is the run-scoped quarantine: a candidate pair is skipped
    when either entity was created at/after the run began, so the run never
    merges its own fresh output (duplicates converge on the next pass once
    established). None disables the quarantine.

    When ``vector_index`` is provided, embedding-near same-type pairs within
    ``semantic_distance_threshold`` (L2) are added to the candidate set,
    catching same-thing-different-name duplicates that share no alias;
    ``semantic_name_gate`` decides how the name signal orders or filters them
    (see :meth:`EntityAbsorption.find_semantic_candidates`).

    ``max_seconds`` (0 = unbounded) is a wall-clock cap on the whole pass,
    candidate generation included: when crossed the pass stops before the
    next pair, emits ``memory.dream.max_seconds_reached`` and leaves the rest
    to the next run. ``judge_concurrency`` judge calls run at once, each in a
    copy of the caller's context so provider telemetry keeps its sink; a
    chunk never holds two pairs that share a page, and merges, cache writes
    and telemetry are applied one pair at a time in candidate order after
    each chunk. The verdict cache is flushed every few pairs and on exit, so
    an interrupted run keeps what it judged.

    Outcomes without a settled verdict — an unparseable reply, ``unclear``,
    a ``same`` below ``confidence_threshold`` — are cached for
    ``recheck_cooldown_s`` and not re-judged until then (0 = every run), so
    a budgeted run advances instead of re-answering the same pairs. A
    provider failure is never cached: it is a property of the moment, and
    after a few in a row the pass stops for this run.

    When ``escalate_floor > 0``, pairs the cheap judge can't settle — verdict
    ``"unclear"``, or ``"same"`` with confidence in ``[escalate_floor,
    confidence_threshold)`` — go to a bounded sub-agent (Tier-2) that
    investigates with the lineage/source tools. Escalation is best-effort: a
    Tier-2 exception keeps the pair rather than aborting the pass.
    ``escalate_floor=0`` disables escalation entirely (old behavior preserved).
    """
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    llm_invoke = llm_invoke or default_llm_invoke
    t0 = _clock()

    def _elapsed_ms() -> int:
        return int((_clock() - t0) * 1000)

    def _over_budget() -> bool:
        return bool(max_seconds) and (_clock() - t0) >= max_seconds

    # Pass the vector index so absorb() keeps it current (drops the absorbed
    # row, re-upserts the canonical) — semantic recall READS this index next
    # run, so a merge must not leave a stale row behind.
    absorber = EntityAbsorption(workspace=workspace, vector_index=vector_index)
    candidates = absorber.find_candidates()
    if vector_index is not None:
        seen = {tuple(sorted(c.refs)) for c in candidates}
        for sc in absorber.find_semantic_candidates(
                vector_index, distance_threshold=semantic_distance_threshold,
                name_gate=semantic_name_gate):
            if tuple(sorted(sc.refs)) not in seen:
                candidates.append(sc)
                seen.add(tuple(sorted(sc.refs)))

    merged: list[dict] = []
    kept: list[dict] = []
    skipped: list[dict] = []
    escalations = 0
    judged_n = 0
    provider_failures = 0
    stop_reason: str | None = None
    tombstones = _load_tombstones(workspace)
    verdict_cache = _load_verdict_cache(workspace)
    # Judge identity: template + model. Either changing re-judges everything.
    judge_id = f"{judge_template_fingerprint()}|{model or ''}"
    unsaved = 0
    last_save = _clock()
    concurrency = max(1, int(judge_concurrency))
    pool = ThreadPoolExecutor(max_workers=concurrency) if concurrency > 1 else None

    def _skip(ref_a: str, ref_b: str, reason: str, detail: str | None = None) -> None:
        skipped.append({"pair": [ref_a, ref_b], "reason": f"{reason}:{detail}" if detail else reason})
        _emit("memory.absorb.skipped", canonical=ref_a, absorbed=ref_b, reason=reason)

    def _flush(force: bool = False) -> None:
        nonlocal unsaved, last_save
        if not unsaved:
            return
        if force or unsaved >= _CACHE_FLUSH_EVERY or _clock() - last_save >= _CACHE_FLUSH_SECONDS:
            _save_verdict_cache(workspace, verdict_cache)
            unsaved = 0
            last_save = _clock()

    def _remember(ref_a: str, ref_b: str, pair_fp: str, **fields) -> None:
        nonlocal unsaved
        verdict_cache[_pair_key(ref_a, ref_b)] = {"fp": pair_fp, "judge": judge_id, "at": _now(), **fields}
        unsaved += 1

    def _prepare(cand) -> dict | None:
        """The pre-judge filters, in candidate order; None = skipped."""
        ref_a, ref_b = cand.refs
        if ref_a.split(":", 1)[0] != ref_b.split(":", 1)[0]:
            _skip(ref_a, ref_b, "cross_type")
            return None
        if _pair_key(ref_a, ref_b) in tombstones:
            _skip(ref_a, ref_b, "tombstoned")
            return None
        page_a = _load_page(workspace, ref_a)
        page_b = _load_page(workspace, ref_b)
        if page_a is None or page_b is None:
            _skip(ref_a, ref_b, "load_failed")
            return None
        if page_a.author == "user_authored" or page_b.author == "user_authored":
            _skip(ref_a, ref_b, "user_managed")
            return None
        if run_started_at is not None and (
                _created_this_run(page_a, run_started_at)
                or _created_this_run(page_b, run_started_at)):
            _skip(ref_a, ref_b, "quarantine")
            return None
        pair_fp = _pair_fingerprint(ref_a, page_a, ref_b, page_b)
        cached = verdict_cache.get(_pair_key(ref_a, ref_b))
        if (cached and cached.get("fp") == pair_fp
                and cached.get("judge") == judge_id):
            until = cached.get("until")
            if until is None:
                _skip(ref_a, ref_b, "cached_verdict")
                return None
            # An entry with an expiry is a verdict still worth re-examining;
            # honour it only while the cooldown is on and not yet elapsed —
            # under the cooldown configured NOW, so lowering the knob
            # shortens what an earlier run remembered.
            try:
                until = float(until)
                at = cached.get("at")
                if at is not None:
                    until = min(until, float(at) + float(recheck_cooldown_s))
            except (TypeError, ValueError):
                until = 0.0
            if recheck_cooldown_s > 0 and until > _now():
                _skip(ref_a, ref_b, "cached_error" if cached.get("verdict") == "error" else "cached_verdict")
                return None
        return {"cand": cand, "a": ref_a, "b": ref_b,
                "page_a": page_a, "page_b": page_b, "fp": pair_fp}

    def _judge_one(item: dict):
        """A judge outcome: a JudgeResult, or the exception that stood in."""
        try:
            return judge_pair(
                item["page_a"], item["page_b"], item["cand"].shared_aliases,
                llm_invoke=llm_invoke, model=model,
                canonical_ref=item["a"], absorbed_ref=item["b"],
                canonical_mtime=_page_mtime(workspace, item["a"]),
                absorbed_mtime=_page_mtime(workspace, item["b"]),
            )
        except JudgeError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001 — one bad page must not end the pass
            return exc

    def _judge_chunk(chunk: list[dict]) -> list:
        if pool is None or len(chunk) == 1:
            return [_judge_one(chunk[0])] if len(chunk) == 1 else [_judge_one(i) for i in chunk]
        # Each worker runs in a copy of this thread's context, so the
        # telemetry sink bound here reaches the provider's own events.
        futures = [pool.submit(contextvars.copy_context().run, _judge_one, item) for item in chunk]
        return [f.result() for f in futures]

    def _apply(item: dict, outcome) -> None:
        nonlocal judged_n, escalations, provider_failures
        ref_a, ref_b, page_a, pair_fp = item["a"], item["b"], item["page_a"], item["fp"]
        cand = item["cand"]
        if isinstance(outcome, Exception):
            error_kind = getattr(outcome, "kind", "pair")
            _skip(ref_a, ref_b, "judge_error", detail=str(outcome))
            if error_kind == "provider":
                provider_failures += 1
                return
            provider_failures = 0
            if recheck_cooldown_s > 0:
                _remember(ref_a, ref_b, pair_fp, verdict="error",
                          error=str(outcome)[:200], until=_now() + float(recheck_cooldown_s))
            return
        provider_failures = 0
        judged = outcome
        judged_n += 1
        _emit("memory.absorb.judged", canonical=ref_a, absorbed=ref_b,
              verdict=judged.verdict, confidence=judged.confidence,
              entity_type=page_a.type,
              distance=cand.distance, name_overlap=cand.name_overlap)
        decision = judged
        escalated = False
        borderline = (
            judged.verdict == "unclear"
            or (judged.verdict == "same"
                and escalate_floor <= judged.confidence < confidence_threshold)
        )
        if escalate_floor and borderline:
            if escalations >= _MAX_ESCALATIONS_PER_RUN:
                _emit("memory.absorb.escalation_capped",
                      canonical=ref_a, absorbed=ref_b)
                add_flagged(workspace, ref_a, ref_b,
                            verdict=judged.verdict,
                            confidence=judged.confidence,
                            reasoning=("escalation cap reached this run; "
                                       "Tier-1 verdict kept — review manually"))
            else:
                escalations += 1
                try:
                    decision = _escalate_judge(workspace, ref_a, ref_b, model=model)
                    escalated = True
                    _emit("memory.absorb.escalated", canonical=ref_a, absorbed=ref_b,
                          verdict=decision.verdict, confidence=decision.confidence)
                except Exception as exc:  # noqa: BLE001 — agent best-effort
                    kept.append({"pair": [ref_a, ref_b], "reason": f"tier2_error:{exc}"})
                    return
        if decision.verdict == "same" and decision.confidence >= confidence_threshold:
            absorber.absorb(
                ref_a, ref_b, reason="refine",
                judge_reasoning=decision.reasoning,
                judge_confidence=decision.confidence,
            )
            merged.append({"canonical": ref_a, "absorbed": ref_b,
                           "confidence": decision.confidence})
            _emit("memory.absorb.auto_merged", canonical=ref_a, absorbed=ref_b,
                  confidence=decision.confidence, entity_type=page_a.type)
            return
        if escalated:
            add_flagged(workspace, ref_a, ref_b,
                        verdict=decision.verdict,
                        confidence=decision.confidence,
                        reasoning=decision.reasoning)
        kept.append({"pair": [ref_a, ref_b], "verdict": decision.verdict,
                     "confidence": decision.confidence})
        if decision.verdict == "different" and not escalated:
            # Settled: the same content gets the same answer next time.
            _remember(ref_a, ref_b, pair_fp, verdict="different",
                      confidence=decision.confidence)
        elif recheck_cooldown_s > 0:
            # Not settled (unclear, below-threshold same, escalated): worth
            # re-examining, but not every run — the scan must advance.
            _remember(ref_a, ref_b, pair_fp, verdict=decision.verdict,
                      confidence=decision.confidence,
                      until=_now() + float(recheck_cooldown_s))

    pos = 0
    try:
        while pos < len(candidates):
            # A chunk never holds two pairs that share a page: a merge is
            # applied before any later pair touching the same page is judged.
            chunk: list[dict] = []
            in_chunk: set[str] = set()
            while pos < len(candidates) and len(chunk) < concurrency:
                if _over_budget():
                    stop_reason = "max_seconds"
                    break
                ref_a, ref_b = candidates[pos].refs
                if chunk and (ref_a in in_chunk or ref_b in in_chunk):
                    break
                item = _prepare(candidates[pos])
                pos += 1
                if item is not None:
                    chunk.append(item)
                    in_chunk.update((ref_a, ref_b))
            # A budget trip stops new work, not work already prepared: the
            # assembled chunk is judged so every candidate consumed by `pos`
            # is accounted for in merged / kept / skipped.
            if chunk:
                for item, outcome in zip(chunk, _judge_chunk(chunk)):
                    _apply(item, outcome)
                _flush()
                if provider_failures >= _MAX_CONSECUTIVE_PROVIDER_FAILURES:
                    stop_reason = "judge_unavailable"
            if stop_reason:
                break
        if stop_reason == "max_seconds":
            _emit("memory.dream.max_seconds_reached", kind="refine",
                  max_seconds=max_seconds, elapsed_ms=_elapsed_ms(),
                  judged=judged_n, remaining=len(candidates) - pos)
            logger.info(
                "refine dream hit max_seconds_per_run ({}s) after {} judged "
                "pair(s) ({}ms); {} candidate(s) wait for the next run",
                max_seconds, judged_n, _elapsed_ms(), len(candidates) - pos)
        elif stop_reason == "judge_unavailable":
            last = next((s for s in reversed(skipped) if s["reason"].startswith("judge_error")), {})
            _emit("memory.absorb.judge_unavailable",
                  consecutive_failures=provider_failures,
                  error_head=str(last.get("reason", ""))[:200],
                  judged=judged_n, remaining=len(candidates) - pos)
            logger.warning(
                "refine dream stopped: {} judge call(s) failed in a row at the "
                "provider; {} candidate(s) wait for the next run",
                provider_failures, len(candidates) - pos)
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
        _flush(force=True)

    return {
        "merged": merged,
        "kept_separate": kept,
        "skipped": skipped,
        "candidates": len(candidates),
        "judged": judged_n,
        "yielded": stop_reason is not None,
        "stop_reason": stop_reason,
    }
