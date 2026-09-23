"""Re-review colliding pairs with the resolution-aware judge.

Two backlogs predate resolutions:

- **pending** — pairs sitting in the Bandeja (``.flagged_pairs.json``), flagged
  when the only choices were merge or keep separate;
- **separated** — pairs the user ruled out as duplicates
  (``.refine_tombstones.json``). "Keep separate" left their shared alias, and
  any vague key, in place.

:func:`run_rereview` sends each pair to the investigating (Tier-2) judge and
treats the answer like the nightly refine does: a confident merge or
resolution is applied, anything short of that lands in the Bandeja with the
proposal attached. For separated pairs the judge is told the user already
decided they are not the same: it may not merge them, and by default its
proposals go to the Bandeja for the user to accept — a human decision is not
overridden silently. ``apply_separated=True`` lets confident proposals apply
directly (the tombstone stays, following any rename).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from loguru import logger

__all__ = ["run_rereview"]


def _default_judge(workspace: Path, ref_a: str, ref_b: str, *, user_kept_separate: bool):
    from durin.memory.tier2_judge import escalate_judge
    return escalate_judge(workspace, ref_a, ref_b, user_kept_separate=user_kept_separate)


def _emit(event: str, **data: Any) -> None:
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(event, data)
    except Exception:  # pragma: no cover — telemetry never breaks the pass
        pass


def run_rereview(
    workspace: Path,
    *,
    pending: bool = True,
    separated: bool = True,
    apply_separated: bool = False,
    dry_run: bool = False,
    limit: int = 0,
    max_seconds: float = 0,
    merge_threshold: int = 80,
    resolve_threshold: int = 85,
    auto_resolve: bool = True,
    auto_rename: bool = True,
    model: str | None = None,
    vector_index: object | None = None,
    judge: Callable[..., Any] | None = None,
) -> dict:
    """Re-judge the Bandeja and/or the user's kept-separate pairs.

    ``merge_threshold`` is the investigating judge's merge floor
    (``auto_absorb.tier2_confidence_threshold``), ``resolve_threshold`` the
    floor for applying a non-merge resolution; ``auto_resolve`` /
    ``auto_rename`` off send those proposals to the Bandeja instead, like the
    nightly pass. Only a ``same`` verdict merges and only a confident
    ``different`` / ``related`` one resolves — an ``unclear`` answer never
    changes memory on its own. ``dry_run`` judges and reports
    without writing anything — not even the Bandeja. ``limit`` caps the pairs
    judged (0 = all), ``max_seconds`` the wall clock. ``judge(workspace,
    ref_a, ref_b, user_kept_separate=…)`` returns a ``JudgeResult`` (tests
    inject one; the default is the Tier-2 sub-agent).

    Returns ``{"pairs": [...], "counts": {...}, "stopped": reason | None}``;
    each pair row has ``group`` (pending | separated), ``pair``, ``verdict``,
    ``confidence``, ``outcome`` (merged | resolved | flagged | settled | kept |
    stale | skipped | error | would_*) and, when it changed keys, ``now``.
    """
    from durin.memory.absorb_judge import judge_template_fingerprint
    from durin.memory.pair_resolution import (
        apply_resolution,
        effective_resolution,
        resolution_from_judge,
    )
    from durin.memory.refine_dream import (
        _actionable,
        _auto_applicable,
        _load_page,
        _load_verdict_cache,
        _now,
        _pair_fingerprint,
        _pair_key,
        _save_verdict_cache,
        add_flagged,
        is_tombstoned,
        read_flagged,
        read_tombstones,
        remove_flagged,
    )

    judge = judge or _default_judge
    t0 = time.perf_counter()
    rows: list[dict] = []
    counts: dict[str, int] = {}
    stopped: str | None = None
    judge_id = f"{judge_template_fingerprint()}|{model or ''}"

    queue: list[tuple[str, str, str]] = []
    if pending:
        for rec in read_flagged(workspace):
            a, b = rec["pair"][0], rec["pair"][1]
            if is_tombstoned(workspace, a, b):
                # The user kept this pair apart: it is the separated pass's
                # (with the judge told so), never a pending merge candidate.
                # This pass's own proposal for such a pair waits for the user;
                # any other record is moot — the decision was already made.
                if rec.get("source") != "rereview" and not dry_run:
                    remove_flagged(workspace, a, b)
                continue
            queue.append(("pending", a, b))
    if separated:
        for a, b in read_tombstones(workspace):
            queue.append(("separated", a, b))

    def _row(group: str, a: str, b: str, outcome: str, **kw: Any) -> None:
        rows.append({"group": group, "pair": [a, b], "outcome": outcome, **kw})
        counts[outcome] = counts.get(outcome, 0) + 1

    def _settle(a: str, b: str, verdict: str, confidence: int) -> None:
        pa, pb = _load_page(workspace, a), _load_page(workspace, b)
        if pa is None or pb is None:
            return
        cache = _load_verdict_cache(workspace)
        cache[_pair_key(a, b)] = {"fp": _pair_fingerprint(a, pa, b, pb), "judge": judge_id,
                                  "at": _now(), "verdict": verdict, "confidence": confidence}
        _save_verdict_cache(workspace, cache)

    judged = 0
    done: set[str] = set()
    for group, a, b in queue:
        key = _pair_key(a, b)
        if key in done:
            continue
        done.add(key)
        if limit and judged >= limit:
            stopped = "limit"
            break
        if max_seconds and time.perf_counter() - t0 >= max_seconds:
            stopped = "max_seconds"
            break
        pa, pb = _load_page(workspace, a), _load_page(workspace, b)
        if pa is None or pb is None:
            # A page was merged, renamed or deleted since: nothing to decide.
            if group == "pending" and not dry_run:
                remove_flagged(workspace, a, b)
            _row(group, a, b, "stale")
            continue
        if pa.author == "user_authored" or pb.author == "user_authored":
            _row(group, a, b, "skipped", reason="user_managed")
            continue
        kept_apart = group == "separated"
        try:
            decision = judge(workspace, a, b, user_kept_separate=kept_apart)
        except Exception as exc:  # noqa: BLE001 — one pair never ends the pass
            logger.warning("rereview: judge failed for {} | {}: {}", a, b, exc)
            _row(group, a, b, "error", error=str(exc)[:200])
            continue
        judged += 1
        verdict, conf = decision.verdict, decision.confidence
        if kept_apart and verdict == "same":
            # The user's decision stands; a "same" here is only a keep.
            verdict = "different"
        try:
            res = resolution_from_judge(verdict, decision.proposal, a, b, confidence=conf,
                                        reasoning=decision.reasoning, source="rereview")
            res = effective_resolution(res, a, b, pa, pb)
        except Exception as exc:  # noqa: BLE001 — an unusable proposal is no proposal
            logger.info("rereview: unusable proposal for {} | {}: {}", a, b, exc)
            res = None
        base = {"verdict": decision.verdict, "confidence": conf,
                "reasoning": decision.reasoning,
                "proposal": res.to_dict() if res else None}

        if res is not None and res.kind == "merge":
            if verdict == "same" and not kept_apart and conf >= merge_threshold:
                if dry_run:
                    _row(group, a, b, "would_merge", **base)
                    continue
                try:
                    out = apply_resolution(workspace, res, a, b, actor="dream",
                                           vector_index=vector_index, allow_rename=auto_rename)
                except Exception as exc:  # noqa: BLE001
                    _row(group, a, b, "error", error=str(exc)[:200], **base)
                    continue
                _emit("memory.absorb.auto_merged", canonical=out.refs[a],
                      absorbed=b if (res.survivor or a) == a else a, confidence=conf,
                      entity_type=pa.type)
                _row(group, a, b, "merged", now=[out.refs[a], out.refs[b]], **base)
                continue
        elif (auto_resolve or kept_apart) and _auto_applicable(
                res, verdict, conf, resolve_threshold, auto_rename) and (
                not kept_apart or apply_separated):
            if dry_run:
                _row(group, a, b, "would_resolve", **base)
                continue
            try:
                out = apply_resolution(workspace, res, a, b, actor="dream",
                                       vector_index=vector_index, allow_rename=auto_rename)
            except Exception as exc:  # noqa: BLE001
                _row(group, a, b, "error", error=str(exc)[:200], **base)
                continue
            new_a, new_b = out.refs[a], out.refs[b]
            _emit("memory.absorb.auto_resolved", canonical=a, absorbed=b, kind=res.kind,
                  confidence=conf, renamed=sorted(r for r, n in out.refs.items() if r != n))
            _settle(new_a, new_b, verdict, conf)
            _row(group, a, b, "resolved", now=[new_a, new_b], **base)
            continue

        if _actionable(res) or (not kept_apart and not (
                verdict in ("different", "related") and conf >= resolve_threshold)):
            # A proposal the user should see, or a pending pair still unsettled.
            if dry_run:
                _row(group, a, b, "would_flag", **base)
                continue
            add_flagged(workspace, a, b, verdict=decision.verdict, confidence=conf,
                        reasoning=decision.reasoning,
                        proposal=res.to_dict() if res else None, source="rereview")
            _row(group, a, b, "flagged", **base)
            continue

        # Confidently distinct with nothing to change.
        if group == "pending" and not dry_run:
            remove_flagged(workspace, a, b)
            _settle(a, b, verdict, conf)
        _row(group, a, b, "settled" if group == "pending" else "kept", **base)

    return {"pairs": rows, "counts": counts, "stopped": stopped,
            "duration_ms": int((time.perf_counter() - t0) * 1000)}
