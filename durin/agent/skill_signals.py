"""Hindsight skill-signal extraction — the dream feeds the observation queue.

The agent rarely calls ``skill_observe`` at runtime (judging mid-task whether a
correction *generalizes* is hard), so the observation queue starves. This pass
detects skill **corrections** and coverage **gaps** in HINDSIGHT — from a
session's full turn trajectory, at dream time — and logs them as observations
the daily curation pass consumes. It is the skill analogue of memory's
``discover_entities``: the agent creates by initiative; the dream discovers in
hindsight. Attribution rides the turn-indexed ``skill_calls`` (which skill was
loaded at which turn), so the prompt does not depend on parsing skill bodies.

Detection only — never mutates a skill. ``log_observation`` dedups and the daily
curation judge (recurrence-weighted, content-judging) decides what to act on.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from json_repair import repair_json

logger = logging.getLogger(__name__)

LLMInvoke = Callable[..., Any]

_VALID_KINDS = ("correction", "gap")

_SKILL_SIGNAL_PROMPT = """You are durin's skill-signal pass. From the conversation \
turns below, identify SKILL feedback worth acting on later — ONLY signals that \
GENERALIZE to future runs, never one-off task nitpicks.

Two kinds:
- "correction": while a skill was loaded (see "SKILLS LOADED" and the SKILL.md \
read in the turns), the user corrected or redirected the output in a way that \
means the SKILL ITSELF should change. Set "skill" to that loaded skill's name.
- "gap": the agent completed a multi-step procedure that NO existing skill \
covers and that is likely to recur. Set "skill" to "new:<short-working-name>".

Rules:
- Only signals that generalize. A correction specific to THIS task (a particular \
value, name, or one-off preference) is NOT a skill signal — skip it.
- A gap is only for work no skill in EXISTING SKILLS covers. If one covers it, \
even in part, report a "correction" on that skill instead. If an OPEN GAP is \
the same procedure, use its name. At most one gap per procedure.
- Ground every signal in the turns. Do not invent.
- For a gap, the improvement states only what the turns show working in the \
end — the commands, queries and names that succeeded — never an attempt that \
failed or a guess the turns did not confirm.
- Each signal is an object with:
  - "skill": the loaded skill's name, or "new:<working-name>" for a gap
  - "kind": "correction" or "gap"
  - "issue": what happened — specific enough to act on weeks later
  - "improvement": the concrete change to the skill (or scope for a new one)
- Output ONLY a JSON array of these objects. If nothing generalizes, output [].

SKILLS LOADED (name @ turn):
{loads}

EXISTING SKILLS (name: what it covers):
{catalog}

OPEN GAPS (already flagged, not yet a skill):
{open_gaps}

CONVERSATION TURNS:
{turns}

JSON:"""

# Bounds on the catalog the pass reads: every skill by name, each described
# in a line, without letting a large catalog crowd out the turns.
_CATALOG_DESCRIPTION_CHARS = 140
_CATALOG_CHARS = 8000
_OPEN_GAPS_SHOWN = 20


def _catalog_text(workspace: Path) -> str:
    from durin.agent.skills_store import list_skills_info

    lines: list[str] = []
    used = 0
    for info in sorted(list_skills_info(workspace), key=lambda s: s["name"]):
        desc = " ".join(str(info.get("description") or "").split())[:_CATALOG_DESCRIPTION_CHARS]
        line = f"- {info['name']}: {desc}" if desc else f"- {info['name']}"
        if used + len(line) > _CATALOG_CHARS:
            lines.append("- (more skills not listed)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) or "(none)"


def _open_gaps_text(workspace: Path) -> str:
    from durin.agent.skill_observations import open_observations

    gaps = [r for r in open_observations(workspace)
            if str(r.get("skill", "")).startswith("new:")][:_OPEN_GAPS_SHOWN]
    return "\n".join(f"- {str(r['skill'])[4:]}: {str(r.get('issue', ''))[:120]}"
                     for r in gaps) or "(none)"


def build_skill_signal_prompt(turns: str, skill_loads: list[dict], *,
                              catalog: str = "(none)", open_gaps: str = "(none)") -> str:
    # A skill counts as loaded whether the agent opened it with skill_view or
    # read its SKILL.md directly; missing either makes a covered procedure
    # look like a gap.
    loads = ", ".join(
        f"{c.get('skill')}@{c.get('turn')}"
        for c in skill_loads
        if c.get("op") in ("read", "view") and c.get("skill")
    ) or "(none recorded)"
    # Tail-truncate: a correction lands AT THE END of an interaction (the user
    # reacts to what the agent just did), so keep the most recent turns — unlike
    # entity discovery, which head-truncates because identity facts come early.
    return _SKILL_SIGNAL_PROMPT.format(loads=loads, catalog=catalog, open_gaps=open_gaps,
                                       turns=turns[-12000:])


def parse_skill_signals(raw: str) -> list[dict]:
    """Tolerant parse of the LLM's JSON array of skill-signal proposals.

    Each item needs a valid ``kind`` (correction|gap) and non-empty
    ``skill``/``issue``/``improvement``; a ``gap`` is normalized to a
    ``new:<name>`` skill ref and a ``correction`` may not be a ``new:`` ref.
    Malformed items are dropped, not raised.
    """
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(repair_json(s))
    except (ValueError, TypeError):
        return []
    if not isinstance(obj, list):
        return []
    out: list[dict] = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("skill", "")).strip()
        kind = str(item.get("kind", "")).strip()
        issue = str(item.get("issue", "")).strip()
        improvement = str(item.get("improvement", "")).strip()
        if kind not in _VALID_KINDS or not skill or not issue or not improvement:
            continue
        if kind == "gap" and not skill.startswith("new:"):
            skill = f"new:{skill}"
        if kind == "correction" and skill.startswith("new:"):
            continue
        out.append({"skill": skill, "kind": kind,
                    "issue": issue, "improvement": improvement})
    return out


def discover_skill_signals(
    workspace: Path,
    turns: str,
    *,
    skill_loads: list[dict] | None = None,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    session: str | None = None,
) -> list[dict]:
    """Detect skill corrections/gaps in ``turns`` and log them as observations.

    Returns the list of logged signals (``{skill, kind, id}``). Empty ``turns``
    makes no LLM call. Each logged signal is deduped by ``log_observation``.
    """
    from durin.agent.skill_observations import log_observation
    from durin.memory.llm_invoke import LLMResponse, default_llm_invoke

    llm_invoke = llm_invoke or default_llm_invoke
    if not turns.strip():
        return []
    prompt = build_skill_signal_prompt(
        turns, skill_loads or [],
        catalog=_catalog_text(workspace), open_gaps=_open_gaps_text(workspace))
    resp = llm_invoke(prompt, model=model) if model else llm_invoke(prompt)
    raw = resp.text if isinstance(resp, LLMResponse) else str(resp)
    signals = parse_skill_signals(raw)

    logged: list[dict] = []
    for sig in signals:
        r = log_observation(
            workspace, skill=sig["skill"], kind=sig["kind"],
            issue=sig["issue"], improvement=sig["improvement"], session=session)
        if r.get("ok"):
            logged.append({**sig, "id": r.get("id")})

    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event("memory.dream.skill_signals", {
            "proposed": len(signals), "logged": len(logged),
            "skills": [s["skill"] for s in logged]})
    except Exception:  # pragma: no cover — telemetry must never break the dream
        pass
    return logged
