"""Extract skill-usage signal (`skill_calls`) from a turn's messages.

A skill "call" is the agent touching a skill during a turn:
- ``read``  — ``read_file`` on ``skills/<name>/SKILL.md`` (progressive load).
- ``edit``  — ``skill_edit`` on a skill (E1 editor).

Each record carries the 1-based ``turn`` index, so a hindsight pass can attribute
"skill X loaded at turn N → user corrected at turn N+1" (skill-signal extraction).

Pure and dependency-free so it's trivially unit-testable and safe to run in the
hot loop. The result is appended to ``session.metadata["skill_calls"]``.
"""
from __future__ import annotations

import json
import re
from typing import Any

_SKILL_PATH_RE = re.compile(r"(?:^|/)skills/([^/]+)/SKILL\.md$")


def _tool_name_and_args(tc: Any) -> tuple[str, dict]:
    fn = tc.get("function") if isinstance(tc, dict) else None
    src = fn if isinstance(fn, dict) else tc
    name = src.get("name", "") if isinstance(src, dict) else ""
    raw = src.get("arguments", {}) if isinstance(src, dict) else {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    return name, raw if isinstance(raw, dict) else {}


def extract_skill_calls(messages: list[dict]) -> list[dict]:
    calls: list[dict] = []
    for i, message in enumerate(messages):
        turn = i + 1                       # messages[i] is turn i+1 (load_session)
        for tc in (message.get("tool_calls") or []):
            name, args = _tool_name_and_args(tc)
            if name == "read_file":
                m = _SKILL_PATH_RE.search(str(args.get("path", "")))
                if m:
                    calls.append({"skill": m.group(1), "op": "read", "turn": turn})
            elif name == "skill_edit":
                skill = args.get("name")
                if skill:
                    calls.append({"skill": skill, "op": "edit", "turn": turn})
    return calls


def collect_recent_skill_calls(workspace, within_hours: float | None = None) -> dict[str, dict[str, int]]:
    """Aggregate skill_calls across session sidecars: {skill: {op: count}}.

    Reads the durable ``derived.skill_calls`` of every session's ``.meta.json``.
    Used by the 2h dream to know which `auto` skills were used (candidates to
    patch). A future per-skill cursor (Part B) bounds this by 'since last';
    Part A reads all present sidecars.

    When ``within_hours`` is set, sidecars whose mtime is older than that window
    are skipped, so the 2h dream can focus on recent activity. Default ``None``
    is unbounded.
    """
    import time as _time
    from pathlib import Path

    from durin.session.session_meta import read_derived

    workspace = Path(workspace)
    sessions_dir = workspace / "sessions"
    agg: dict[str, dict[str, int]] = {}
    if not sessions_dir.is_dir():
        return agg
    cutoff = (_time.time() - within_hours * 3600) if within_hours is not None else None
    for meta in sessions_dir.glob("*.meta.json"):
        try:
            if cutoff is not None and meta.stat().st_mtime < cutoff:
                continue
            derived = read_derived(meta)
        except Exception:
            continue
        for call in (derived.get("skill_calls") or []):
            skill = call.get("skill")
            op = call.get("op")
            if not skill or not op:
                continue
            agg.setdefault(skill, {}).setdefault(op, 0)
            agg[skill][op] += 1
    return agg


def compute_working_set(
    workspace,
    candidates: list[str],
    *,
    recent: int,
    frequent: int,
    frequent_window_hours: float = 168.0,
    recent_window_hours: float = 24.0,
) -> list[str]:
    """Usage-ranked working set of skill names for the hot tier.

    Top ``frequent`` candidates by call-count over ``frequent_window_hours``
    (the durable working set), then top ``recent`` over ``recent_window_hours``,
    deduped. Then fill to ``frequent + recent``: first with any remaining
    *used* candidates (by combined count) so a used skill never loses a slot
    to an unused one, then with the rest in stable ``candidates`` order so a
    small/cold catalog still injects something. Usage for names not in
    ``candidates`` is ignored. Returns at most ``frequent + recent`` names.
    """
    cand_set = set(candidates)

    def _totals(window: float, top: int) -> dict[str, int]:
        if top <= 0:
            return {}
        agg = collect_recent_skill_calls(workspace, within_hours=window)
        return {
            s: sum(ops.values())
            for s, ops in agg.items()
            if s in cand_set
        }

    def _top(totals: dict[str, int], top: int) -> list[str]:
        ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
        return [s for s, _ in ordered[:max(0, top)]]

    freq_totals = _totals(frequent_window_hours, frequent)
    rec_totals = _totals(recent_window_hours, recent)

    out: list[str] = []
    seen: set[str] = set()
    for name in (*_top(freq_totals, frequent), *_top(rec_totals, recent)):
        if name not in seen:
            seen.add(name)
            out.append(name)

    budget = max(0, recent) + max(0, frequent)
    if len(out) < budget:
        # Fill: prefer remaining *used* candidates (by combined count) over
        # never-used ones — a used skill must not lose a slot to an unused one
        # when the budget is below the used-set size. Then any remaining slots
        # go to unused candidates in stable catalog order.
        combined: dict[str, int] = {}
        for s in cand_set:
            c = freq_totals.get(s, 0) + rec_totals.get(s, 0)
            if c > 0:
                combined[s] = c
        used_rest = [
            s for s in sorted(combined, key=lambda s: (-combined[s], s))
            if s not in seen
        ]
        for name in (*used_rest, *candidates):
            if len(out) >= budget:
                break
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out[:budget]
