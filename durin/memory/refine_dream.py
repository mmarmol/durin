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

__all__ = ["is_tombstoned", "add_tombstone", "run_refine"]

LLMInvoke = Callable[..., Any]
_TOMBSTONE_FILE = ".refine_tombstones.json"


def _emit(event: str, **data: Any) -> None:
    """Best-effort dream telemetry (reuses the legacy memory.absorb.* names)."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(event, data)
    except Exception:  # pragma: no cover — telemetry must never break the dream
        pass


def _too_fresh(page: "EntityPage", min_age_hours: int, now: Any) -> bool:
    """True when the entity is younger than the quarantine window (B3). Uses
    created_at, falling back to updated_at; with no timestamp the entity is
    treated as old (never quarantined — fail open, don't block a merge)."""
    from datetime import timedelta
    ts = page.created_at or page.updated_at
    if ts is None:
        return False
    return (now - ts) < timedelta(hours=min_age_hours)


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


def run_refine(
    workspace: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    confidence_threshold: int = 95,
    min_age_hours: int = 0,
) -> dict:
    """Dedup pass: judge alias-overlap candidate pairs and merge the same ones.

    ``min_age_hours`` (B3) quarantines freshly-created/edited entities: a
    candidate pair is skipped when either entity is younger than the window, so
    the dream doesn't merge two entities before they have differentiated. 0
    disables the quarantine.
    """
    from datetime import datetime, timezone
    llm_invoke = llm_invoke or default_llm_invoke
    now = datetime.now(timezone.utc)
    absorber = EntityAbsorption(workspace=workspace)
    candidates = absorber.find_candidates()

    merged: list[dict] = []
    kept: list[dict] = []
    skipped: list[dict] = []

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
        if min_age_hours and (_too_fresh(page_a, min_age_hours, now)
                              or _too_fresh(page_b, min_age_hours, now)):
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
            continue
        _emit("memory.absorb.judged", canonical=ref_a, absorbed=ref_b,
              verdict=judged.verdict, confidence=judged.confidence)
        if judged.verdict == "same" and judged.confidence >= confidence_threshold:
            absorber.absorb(
                ref_a, ref_b, reason="refine",
                judge_reasoning=judged.reasoning,
                judge_confidence=judged.confidence,
            )
            merged.append({"canonical": ref_a, "absorbed": ref_b,
                           "confidence": judged.confidence})
            _emit("memory.absorb.auto_merged", canonical=ref_a, absorbed=ref_b,
                  confidence=judged.confidence)
        else:
            kept.append({"pair": [ref_a, ref_b], "verdict": judged.verdict,
                         "confidence": judged.confidence})

    return {
        "merged": merged,
        "kept_separate": kept,
        "skipped": skipped,
        "candidates": len(candidates),
    }
