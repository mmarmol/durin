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
from pathlib import Path
from typing import Any, Callable

from durin.memory.absorb_judge import JudgeError, judge_pair
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
    """Record a pair the Tier-2 agent investigated but did not confirm as same.

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
    return EntityPage.from_file(path) if path.exists() else None


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
) -> dict:
    """Dedup pass: judge alias-overlap candidate pairs and merge the same ones.

    ``run_started_at`` is the run-scoped quarantine: a candidate pair is skipped
    when either entity was created at/after the run began, so the run never
    merges its own fresh output (duplicates converge on the next pass once
    established). None disables the quarantine.

    When ``vector_index`` is provided, embedding-near same-type pairs within
    ``semantic_distance_threshold`` (L2) are added to the candidate set,
    catching same-thing-different-name duplicates that share no alias.

    When ``escalate_floor > 0``, pairs the cheap judge can't settle — verdict
    ``"unclear"``, or ``"same"`` with confidence in ``[escalate_floor,
    confidence_threshold)`` — go to a bounded sub-agent (Tier-2) that
    investigates with the lineage/source tools. Escalation is best-effort: a
    Tier-2 exception keeps the pair rather than aborting the pass.
    ``escalate_floor=0`` disables escalation entirely (old behavior preserved).
    """
    llm_invoke = llm_invoke or default_llm_invoke
    # Pass the vector index so absorb() keeps it current (drops the absorbed
    # row, re-upserts the canonical) — semantic recall READS this index next
    # run, so a merge must not leave a stale row behind.
    absorber = EntityAbsorption(workspace=workspace, vector_index=vector_index)
    candidates = absorber.find_candidates()
    if vector_index is not None:
        seen = {tuple(sorted(c.refs)) for c in candidates}
        for sc in absorber.find_semantic_candidates(
                vector_index, distance_threshold=semantic_distance_threshold):
            if tuple(sorted(sc.refs)) not in seen:
                candidates.append(sc)
                seen.add(tuple(sorted(sc.refs)))

    merged: list[dict] = []
    kept: list[dict] = []
    skipped: list[dict] = []
    escalations = 0

    for cand in candidates:
        ref_a, ref_b = cand.refs
        if ref_a.split(":", 1)[0] != ref_b.split(":", 1)[0]:
            skipped.append({"pair": [ref_a, ref_b], "reason": "cross_type"})
            _emit("memory.absorb.skipped", canonical=ref_a, absorbed=ref_b, reason="cross_type")
            continue
        if is_tombstoned(workspace, ref_a, ref_b):
            skipped.append({"pair": [ref_a, ref_b], "reason": "tombstoned"})
            _emit("memory.absorb.skipped", canonical=ref_a, absorbed=ref_b, reason="tombstoned")
            continue
        page_a = _load_page(workspace, ref_a)
        page_b = _load_page(workspace, ref_b)
        if page_a is None or page_b is None:
            skipped.append({"pair": [ref_a, ref_b], "reason": "load_failed"})
            continue
        if page_a.author == "user_authored" or page_b.author == "user_authored":
            skipped.append({"pair": [ref_a, ref_b], "reason": "user_managed"})
            _emit("memory.absorb.skipped", canonical=ref_a, absorbed=ref_b, reason="user_managed")
            continue
        if run_started_at is not None and (
                _created_this_run(page_a, run_started_at)
                or _created_this_run(page_b, run_started_at)):
            skipped.append({"pair": [ref_a, ref_b], "reason": "quarantine"})
            _emit("memory.absorb.skipped", canonical=ref_a, absorbed=ref_b, reason="quarantine")
            continue
        try:
            judged = judge_pair(
                page_a, page_b, cand.shared_aliases,
                llm_invoke=llm_invoke, model=model,
                canonical_ref=ref_a, absorbed_ref=ref_b,
                canonical_mtime=_page_mtime(workspace, ref_a),
                absorbed_mtime=_page_mtime(workspace, ref_b),
            )
        except JudgeError as exc:
            skipped.append({"pair": [ref_a, ref_b], "reason": f"judge_error:{exc}"})
            _emit("memory.absorb.skipped", canonical=ref_a, absorbed=ref_b,
                  reason="judge_error")
            continue
        _emit("memory.absorb.judged", canonical=ref_a, absorbed=ref_b,
              verdict=judged.verdict, confidence=judged.confidence,
              entity_type=page_a.type,
              distance=cand.distance)
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
                    continue
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
        else:
            if escalated:
                add_flagged(workspace, ref_a, ref_b,
                            verdict=decision.verdict,
                            confidence=decision.confidence,
                            reasoning=decision.reasoning)
            kept.append({"pair": [ref_a, ref_b], "verdict": decision.verdict,
                         "confidence": decision.confidence})

    return {
        "merged": merged,
        "kept_separate": kept,
        "skipped": skipped,
        "candidates": len(candidates),
    }
