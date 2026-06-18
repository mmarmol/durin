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
- "gap": the agent completed a multi-step procedure that NO loaded skill covers \
and that is likely to recur. Set "skill" to "new:<short-working-name>".

Rules:
- Only signals that generalize. A correction specific to THIS task (a particular \
value, name, or one-off preference) is NOT a skill signal — skip it.
- Ground every signal in the turns. Do not invent.
- Each signal is an object with:
  - "skill": the loaded skill's name, or "new:<working-name>" for a gap
  - "kind": "correction" or "gap"
  - "issue": what happened — specific enough to act on weeks later
  - "improvement": the concrete change to the skill (or scope for a new one)
- Output ONLY a JSON array of these objects. If nothing generalizes, output [].

SKILLS LOADED (name @ turn):
{loads}

CONVERSATION TURNS:
{turns}

JSON:"""


def build_skill_signal_prompt(turns: str, skill_loads: list[dict]) -> str:
    loads = ", ".join(
        f"{c.get('skill')}@{c.get('turn')}"
        for c in skill_loads
        if c.get("op") == "read" and c.get("skill")
    ) or "(none recorded)"
    return _SKILL_SIGNAL_PROMPT.format(loads=loads, turns=turns[:12000])


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
    prompt = build_skill_signal_prompt(turns, skill_loads or [])
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
