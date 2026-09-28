"""Extract skill-usage signal (`skill_calls`) from a turn's messages.

A skill "call" is the agent touching a skill during a turn:
- ``view``  — ``skill_view`` on a skill (the dedicated load tool).
- ``read``  — ``read_file`` on ``skills/<name>/SKILL.md`` (raw-read fallback).
- ``edit``  — ``skill_edit`` on a skill (E1 editor).

Each record carries the 1-based ``turn`` index, so a hindsight pass can attribute
"skill X loaded at turn N → user corrected at turn N+1" (skill-signal extraction).

Pure and dependency-free so it's trivially unit-testable and safe to run in the
hot loop. The result is appended to ``session.metadata["skill_calls"]``.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

_SKILL_PATH_RE = re.compile(r"(?:^|/)skills/([^/]+)/SKILL\.md$")
# A file inside a skill's folder, other than its SKILL.md: a bundled script.
_SKILL_FILE_RE = re.compile(r"(?:^|/)skills/([A-Za-z0-9._-]+)/(?!SKILL\.md$)[^/].*[^/]$")
_EXIT_CODE_RE = re.compile(r"Exit code: (-?\d+)")
# Shell structure around the program a command segment runs.
_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[|;&\n]")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_WRAPPERS = frozenset({"sudo", "env", "time", "nice", "timeout", "nohup", "exec", "command"})
_INTERPRETER_RE = re.compile(
    r"^(?:python\d*(?:\.\d+)?|bash|sh|zsh|dash|node|deno|bun|tsx|ruby|perl|php|uv|uvx)$")


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


def emit_skill_used(calls: list[dict], session_key: str | None = None) -> None:
    """Emit one ``skill.used`` event per skill call (best-effort).

    Called from ``AgentLoop._state_save`` right after ``calls`` are recorded
    into ``session.metadata["skill_calls"]``. That runs after the turn's run
    has released its telemetry binding, so with no binding current the events
    go to ``session_key``'s own logger instead of being dropped.
    """
    if not calls:
        return
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        from durin.telemetry.logger import (
            bind_telemetry,
            current_telemetry,
            get_session_logger,
            reset_telemetry,
        )

        token = None
        if current_telemetry() is None and session_key:
            token = bind_telemetry(get_session_logger(session_key))
        try:
            for call in calls:
                emit_tool_event("skill.used", dict(call))
        finally:
            if token is not None:
                reset_telemetry(token)
    except Exception:  # noqa: BLE001 — telemetry must never break the loop
        pass


def _run_outcome(result: Any) -> bool | None:
    """Whether an exec result says the command succeeded: its last
    ``Exit code:`` line, or None when the result does not show one."""
    if not isinstance(result, str):
        return None
    codes = _EXIT_CODE_RE.findall(result)
    return int(codes[-1]) == 0 if codes else None


def _invoked_program(segment: str) -> str | None:
    """The file a command segment runs: its program, or the first argument of
    an interpreter program (``python3 -u x.py``), past env assignments and
    wrappers (``timeout 60``)."""
    try:
        words = [w for w in shlex.split(segment) if not _ENV_ASSIGN_RE.match(w)]
    except ValueError:
        return None
    while words and (words[0] in _WRAPPERS or _DURATION_RE.match(words[0])):
        words = words[1:]
    if not words:
        return None
    program = Path(words[0]).name
    if not _INTERPRETER_RE.match(program):
        return words[0]
    args = [w for w in words[1:] if not w.startswith("-")]
    if program in ("uv", "uvx") and args[:1] == ["run"]:
        args = args[1:]
    return args[0] if args else None


def _skill_of_script(path: str, workspace: Path | None) -> str | None:
    """The skill whose bundled file ``path`` is. With ``workspace``, the file
    must exist in that workspace's skills folder (a ``skills/`` folder of
    another repo is not this workspace's skill)."""
    m = _SKILL_FILE_RE.search(path)
    if not m:
        return None
    if workspace is None:
        return m.group(1)
    skills_dir = (Path(workspace) / "skills").resolve()
    candidate = Path(path) if Path(path).is_absolute() else Path(workspace) / path
    try:
        rel = candidate.resolve().relative_to(skills_dir)
    except ValueError:
        return None
    return rel.parts[0] if len(rel.parts) > 1 and candidate.is_file() else None


def _script_runs(command: str, workspace: Path | None) -> tuple[list[str], bool]:
    """The skills whose bundled scripts ``command`` runs, and whether the
    command is that one run alone (so its exit code is the script's)."""
    segments = [s for s in _SEGMENT_SPLIT_RE.split(command) if s.strip()]
    skills: list[str] = []
    for segment in segments:
        program = _invoked_program(segment)
        skill = _skill_of_script(program, workspace) if program else None
        if skill and skill not in skills:
            skills.append(skill)
    return skills, len(segments) == 1


def extract_skill_calls(messages: list[dict], workspace: Path | None = None) -> list[dict]:
    calls: list[dict] = []
    results = {m.get("tool_call_id"): m.get("content")
               for m in messages if m.get("role") == "tool" and m.get("tool_call_id")}
    for i, message in enumerate(messages):
        turn = i + 1                       # messages[i] is turn i+1 (load_session)
        for tc in (message.get("tool_calls") or []):
            name, args = _tool_name_and_args(tc)
            if name == "exec":
                # Running a skill's bundled script is use of that skill, and
                # its exit code says whether the script works — when the
                # command is that run alone, not a pipeline or a chain.
                skills, alone = _script_runs(str(args.get("command", "")), workspace)
                ok = _run_outcome(results.get(tc.get("id")) if isinstance(tc, dict) else None)
                for skill in skills:
                    call = {"skill": skill, "op": "run", "turn": turn}
                    if ok is not None and alone:
                        call["ok"] = ok
                    calls.append(call)
            elif name == "skill_view":
                skill = args.get("name")
                if skill:
                    calls.append({"skill": skill, "op": "view", "turn": turn})
            elif name == "read_file":
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


def collect_usage_and_last_used(
    workspace, within_hours: float | None = None,
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """One pass over the session sidecars: (op counts, newest mtime) per skill.

    Same aggregation as :func:`collect_recent_skill_calls` plus the newest
    sidecar mtime (epoch ms) per skill named in its ``skill_calls`` — the
    webui's "last used" signal. Combined into one glob+read pass so the
    skills-list endpoint doesn't scan ``sessions/*.meta.json`` twice.
    """
    import time as _time
    from pathlib import Path

    from durin.session.session_meta import read_derived

    workspace = Path(workspace)
    sessions_dir = workspace / "sessions"
    agg: dict[str, dict[str, int]] = {}
    last_used_ms: dict[str, int] = {}
    if not sessions_dir.is_dir():
        return agg, last_used_ms
    cutoff = (_time.time() - within_hours * 3600) if within_hours is not None else None
    for meta in sessions_dir.glob("*.meta.json"):
        try:
            mtime = meta.stat().st_mtime
            if cutoff is not None and mtime < cutoff:
                continue
            derived = read_derived(meta)
        except Exception:
            continue
        mtime_ms = int(mtime * 1000)
        for call in (derived.get("skill_calls") or []):
            skill = call.get("skill")
            op = call.get("op")
            if not skill or not op:
                continue
            agg.setdefault(skill, {}).setdefault(op, 0)
            agg[skill][op] += 1
            if mtime_ms > last_used_ms.get(skill, 0):
                last_used_ms[skill] = mtime_ms
    return agg, last_used_ms


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
