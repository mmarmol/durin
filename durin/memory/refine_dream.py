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
import re
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


_now = time.time  # module-level so tests can move the clock

_NAME_TOKEN_RE = re.compile(r"[\W_]+")


def _name_forms(ref: str, page: "EntityPage") -> list[str]:
    forms = [ref.split(":", 1)[1], page.name or "", *(page.aliases or [])]
    return [f.lower() for f in forms if f]


def _names_overlap(ref_a: str, page_a: "EntityPage", ref_b: str, page_b: "EntityPage") -> bool:
    """Structural name evidence for an embedding-near pair.

    True when the two entities share a name token (slug, name or alias,
    tokens of three or more characters) or one compact form — the name
    with separators stripped — contains the other (``email-flow`` /
    ``emailflow``, ``auto-filling`` / ``mxhero-autofilling-system``). No
    vocabulary, no language assumptions: purely how the two are written.
    """
    fa, fb = _name_forms(ref_a, page_a), _name_forms(ref_b, page_b)

    def toks(forms: list[str]) -> set[str]:
        return {t for f in forms for t in _NAME_TOKEN_RE.split(f) if len(t) >= 3}

    if toks(fa) & toks(fb):
        return True
    ca = {_NAME_TOKEN_RE.sub("", f) for f in fa}
    cb = {_NAME_TOKEN_RE.sub("", f) for f in fb}
    return any(
        len(x) >= 4 and len(y) >= 4 and (x in y or y in x)
        for x in ca for y in cb
    )


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
    max_seconds: float = 0,
    judge_concurrency: int = 1,
    error_cooldown_s: float = 7 * 86400,
    require_name_overlap: bool = True,
) -> dict:
    """Dedup pass: judge alias-overlap candidate pairs and merge the same ones.

    ``run_started_at`` is the run-scoped quarantine: a candidate pair is skipped
    when either entity was created at/after the run began, so the run never
    merges its own fresh output (duplicates converge on the next pass once
    established). None disables the quarantine.

    When ``vector_index`` is provided, embedding-near same-type pairs within
    ``semantic_distance_threshold`` (L2) are added to the candidate set,
    catching same-thing-different-name duplicates that share no alias. With
    ``require_name_overlap`` those pairs are judged only when the two names
    show structural overlap (see :func:`_names_overlap`); alias pairs always
    are.

    ``max_seconds`` (0 = unbounded) is a wall-clock cap: when crossed the pass
    stops before the next chunk, emits ``memory.dream.max_seconds_reached``
    and leaves the rest to the next run — the verdict cache makes that
    incremental. ``judge_concurrency`` judge calls run at once; merges,
    tombstone checks and cache writes are applied one at a time in candidate
    order after each chunk, and a merge earlier in the chunk is re-checked
    before a later one touches the same page. The cache is saved after every
    chunk, so an interrupted run keeps what it judged. A pair whose judge call
    failed is cached with ``error_cooldown_s`` and not re-judged until it
    expires (0 = retry every run).

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

    t0 = time.perf_counter()
    merged: list[dict] = []
    kept: list[dict] = []
    skipped: list[dict] = []
    escalations = 0
    judged_n = 0
    budget_hit = False
    verdict_cache = _load_verdict_cache(workspace)
    # Judge identity: template + model. Either changing re-judges everything.
    judge_id = f"{judge_template_fingerprint()}|{model or ''}"
    cache_dirty = False
    concurrency = max(1, int(judge_concurrency))

    def _skip(ref_a: str, ref_b: str, reason: str) -> None:
        skipped.append({"pair": [ref_a, ref_b], "reason": reason})
        _emit("memory.absorb.skipped", canonical=ref_a, absorbed=ref_b, reason=reason)

    def _prepare(cand) -> dict | None:
        """The pre-judge filters, in candidate order; None = skipped."""
        ref_a, ref_b = cand.refs
        if ref_a.split(":", 1)[0] != ref_b.split(":", 1)[0]:
            _skip(ref_a, ref_b, "cross_type")
            return None
        if is_tombstoned(workspace, ref_a, ref_b):
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
        if (require_name_overlap and cand.distance is not None
                and not cand.shared_aliases
                and not _names_overlap(ref_a, page_a, ref_b, page_b)):
            _skip(ref_a, ref_b, "no_name_overlap")
            return None
        pair_fp = _pair_fingerprint(ref_a, page_a, ref_b, page_b)
        cached = verdict_cache.get(_pair_key(ref_a, ref_b))
        if (cached and cached.get("fp") == pair_fp
                and cached.get("judge") == judge_id):
            if cached.get("verdict") == "error":
                if float(cached.get("until") or 0) > _now():
                    _skip(ref_a, ref_b, "cached_error")
                    return None
            else:
                _skip(ref_a, ref_b, "cached_verdict")
                return None
        return {"cand": cand, "a": ref_a, "b": ref_b,
                "page_a": page_a, "page_b": page_b, "fp": pair_fp}

    def _judge_one(item: dict):
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

    def _judge_chunk(chunk: list[dict]) -> list:
        if len(chunk) == 1:
            return [_judge_one(chunk[0])]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(chunk)) as pool:
            return list(pool.map(_judge_one, chunk))

    pos = 0
    try:
        while pos < len(candidates):
            if max_seconds and (time.perf_counter() - t0) >= max_seconds:
                budget_hit = True
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                _emit("memory.dream.max_seconds_reached", kind="refine",
                      max_seconds=max_seconds, elapsed_ms=elapsed_ms,
                      judged=judged_n, remaining=len(candidates) - pos)
                logger.info(
                    "refine dream hit max_seconds_per_run ({}s) after {} judged "
                    "pair(s) ({}ms); {} candidate(s) wait for the next run",
                    max_seconds, judged_n, elapsed_ms, len(candidates) - pos)
                break
            chunk: list[dict] = []
            while pos < len(candidates) and len(chunk) < concurrency:
                item = _prepare(candidates[pos])
                pos += 1
                if item is not None:
                    chunk.append(item)
            if not chunk:
                continue
            for item, outcome in zip(chunk, _judge_chunk(chunk)):
                ref_a, ref_b, page_a, pair_fp = item["a"], item["b"], item["page_a"], item["fp"]
                cand = item["cand"]
                if isinstance(outcome, JudgeError):
                    skipped.append({"pair": [ref_a, ref_b], "reason": f"judge_error:{outcome}"})
                    _emit("memory.absorb.skipped", canonical=ref_a, absorbed=ref_b,
                          reason="judge_error")
                    if error_cooldown_s > 0:
                        verdict_cache[_pair_key(ref_a, ref_b)] = {
                            "verdict": "error",
                            "error": str(outcome)[:200],
                            "fp": pair_fp,
                            "judge": judge_id,
                            "until": _now() + float(error_cooldown_s),
                        }
                        cache_dirty = True
                    continue
                judged = outcome
                judged_n += 1
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
                    # A merge earlier in this chunk may have absorbed one of
                    # these pages after it was judged: re-check before merging.
                    if (is_tombstoned(workspace, ref_a, ref_b)
                            or _load_page(workspace, ref_a) is None
                            or _load_page(workspace, ref_b) is None):
                        _skip(ref_a, ref_b, "merged_earlier")
                        continue
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
                    if decision.verdict == "different" and not escalated:
                        verdict_cache[_pair_key(ref_a, ref_b)] = {
                            "verdict": decision.verdict,
                            "confidence": decision.confidence,
                            "fp": pair_fp,
                            "judge": judge_id,
                        }
                        cache_dirty = True
            if cache_dirty:
                _save_verdict_cache(workspace, verdict_cache)
                cache_dirty = False
    finally:
        if cache_dirty:
            _save_verdict_cache(workspace, verdict_cache)

    return {
        "merged": merged,
        "kept_separate": kept,
        "skipped": skipped,
        "candidates": len(candidates),
        "judged": judged_n,
        "budget_hit": budget_hit,
    }
