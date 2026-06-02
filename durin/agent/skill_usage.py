"""Extract skill-usage signal (`skill_calls`) from a turn's messages.

A skill "call" is the agent touching a skill during a turn:
- ``read``  — ``read_file`` on ``skills/<name>/SKILL.md`` (progressive load).
- ``edit``  — ``skill_edit`` on a skill (E1 editor).

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
    for message in messages:
        for tc in (message.get("tool_calls") or []):
            name, args = _tool_name_and_args(tc)
            if name == "read_file":
                m = _SKILL_PATH_RE.search(str(args.get("path", "")))
                if m:
                    calls.append({"skill": m.group(1), "op": "read"})
            elif name == "skill_edit":
                skill = args.get("name")
                if skill:
                    calls.append({"skill": skill, "op": "edit"})
    return calls


def collect_recent_skill_calls(workspace) -> dict[str, dict[str, int]]:
    """Aggregate skill_calls across session sidecars: {skill: {op: count}}.

    Reads the durable ``derived.skill_calls`` of every session's ``.meta.json``.
    Used by the 2h dream to know which `auto` skills were used (candidates to
    patch). A future per-skill cursor (Part B) bounds this by 'since last';
    Part A reads all present sidecars.
    """
    from pathlib import Path

    from durin.session.session_meta import read_derived

    workspace = Path(workspace)
    sessions_dir = workspace / "sessions"
    agg: dict[str, dict[str, int]] = {}
    if not sessions_dir.is_dir():
        return agg
    for meta in sessions_dir.glob("*.meta.json"):
        try:
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
