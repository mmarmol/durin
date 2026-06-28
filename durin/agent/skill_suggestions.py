# durin/agent/skill_suggestions.py
"""Skill-suggestion lifecycle: the dream's curation analysis for MANUAL skills,
surfaced for user review instead of applied.

Mirrors the refine pass's flagged-pairs store: a JSON queue keyed by a stable
fingerprint, plus an *expiring* rejection tombstone (the cara-B of "don't
discard the analysis": a rejected conclusion is silenced for a window, then may
re-surface). A per-skill evaluation cursor lets the generation pass skip
unchanged manual skills WITHOUT writing to the user's skill files.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 30


def _skills_dir(workspace: Path) -> Path:
    return Path(workspace) / "skills"


def _suggestions_path(workspace: Path) -> Path:
    return _skills_dir(workspace) / ".suggestions.json"


def _tombstones_path(workspace: Path) -> Path:
    return _skills_dir(workspace) / ".suggestion_tombstones.json"


def _cursor_path(workspace: Path) -> Path:
    return _skills_dir(workspace) / ".suggestion_cursor.json"


def _normalize(s: str) -> str:
    return " ".join((s or "").split())


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def fingerprint(action: dict) -> str:
    """Stable identity of a conclusion: action + target + a normalized signature
    of the proposed change. Deliberately independent of the rationale text — the
    judge may word the same idea differently run to run."""
    t = action.get("type")
    if t == "evolve":
        sig = "evolve|{}|{}|{}".format(
            action.get("name"), action.get("file", "SKILL.md"),
            _normalize(action.get("new", "")))
    elif t == "retire":
        sig = "retire|{}".format(action.get("name"))
    elif t == "fuse":
        sig = "fuse|{}|{}".format(
            action.get("target"), "|".join(sorted(action.get("sources", []))))
    else:
        sig = json.dumps(action, sort_keys=True, ensure_ascii=False)
    return _hash(sig)[:16]


def make_patch(action: dict) -> str | None:
    """Unified diff for display; None when the action has no content change."""
    t = action.get("type")
    if t == "evolve":
        file = action.get("file", "SKILL.md")
        old = (action.get("old", "")).splitlines()
        new = (action.get("new", "")).splitlines()
        diff = difflib.unified_diff(
            old, new, fromfile=f"a/{file}", tofile=f"b/{file}", lineterm="")
        body = "\n".join(diff)
        return body + "\n" if body else None
    if t == "fuse":
        file = "SKILL.md"
        target = action.get("target", "skill")
        new = (action.get("content", "")).splitlines()
        diff = difflib.unified_diff(
            [], new, fromfile=f"a/{target}/{file}", tofile=f"b/{target}/{file}",
            lineterm="")
        body = "\n".join(diff)
        return body + "\n" if body else None
    return None


def _action_skill(action: dict) -> str:
    return action.get("name") or action.get("target") or ""


def read_suggestions(workspace: Path) -> list[dict]:
    p = _suggestions_path(workspace)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def get_suggestion(workspace: Path, fingerprint_: str) -> dict | None:
    for rec in read_suggestions(workspace):
        if rec.get("id") == fingerprint_:
            return rec
    return None


def add_suggestion(workspace: Path, action: dict) -> dict:
    """Enqueue a suggestion (newest wins per fingerprint). Returns the record."""
    fp = fingerprint(action)
    rec = {
        "id": fp,
        "skill": _action_skill(action),
        "type": action.get("type"),
        "reason": action.get("rationale", ""),
        "patch": make_patch(action),
        "action": action,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    records: dict[str, dict] = {}
    p = _suggestions_path(workspace)
    if p.exists():
        try:
            for r in json.loads(p.read_text(encoding="utf-8")):
                records[r["id"]] = r
        except Exception:
            records = {}
    records[fp] = rec
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(list(records.values()), indent=2))
    except Exception:  # pragma: no cover — a store error must not break the pass
        logger.exception("failed to write suggestion queue")
    return rec


def remove_suggestion(workspace: Path, fingerprint_: str) -> None:
    p = _suggestions_path(workspace)
    if not p.exists():
        return
    try:
        records = {r["id"]: r for r in json.loads(p.read_text(encoding="utf-8"))}
    except Exception:
        return
    if fingerprint_ not in records:
        return
    del records[fingerprint_]
    try:
        atomic_write_text(p, json.dumps(list(records.values()), indent=2))
    except Exception:  # pragma: no cover
        logger.exception("failed to rewrite suggestion queue")


def add_tombstone(workspace: Path, fingerprint_: str) -> None:
    """Record a rejected conclusion with its timestamp (for TTL-based expiry)."""
    p = _tombstones_path(workspace)
    data: dict[str, str] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[fingerprint_] = datetime.now(tz=timezone.utc).isoformat()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(data, indent=2))
    except Exception:  # pragma: no cover
        logger.exception("failed to write suggestion tombstones")


def is_tombstoned(workspace: Path, fingerprint_: str, *,
                  ttl_days: int = DEFAULT_TTL_DAYS) -> bool:
    """True when a non-expired rejection tombstone covers this conclusion.
    Expired entries are purged lazily."""
    p = _tombstones_path(workspace)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    now = datetime.now(tz=timezone.utc)
    live: dict[str, str] = {}
    blocked = False
    for fp, ts in data.items():
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        age_days = (now - dt).total_seconds() / 86400.0
        if age_days < ttl_days:
            live[fp] = ts
            if fp == fingerprint_:
                blocked = True
    if len(live) != len(data):
        try:
            atomic_write_text(p, json.dumps(live, indent=2))
        except Exception:  # pragma: no cover
            pass
    return blocked


def _body_hash(workspace: Path, name: str) -> str | None:
    from durin.agent import skills_store as ss
    content = ss.read_skill_content(workspace, name)
    if content is None:
        return None
    return _hash(content)


def needs_suggestion(workspace: Path, name: str) -> bool:
    """True when the skill is new or its content changed since last evaluated —
    tracked in a sidecar cursor so the manual skill file is never written."""
    cur = _body_hash(workspace, name)
    if cur is None:
        return False
    p = _cursor_path(workspace)
    stored = None
    if p.exists():
        try:
            stored = json.loads(p.read_text(encoding="utf-8")).get(name)
        except Exception:
            stored = None
    return stored != cur


def mark_suggested(workspace: Path, name: str) -> None:
    cur = _body_hash(workspace, name)
    if cur is None:
        return
    p = _cursor_path(workspace)
    data: dict[str, str] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[name] = cur
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, json.dumps(data, indent=2))
    except Exception:  # pragma: no cover
        logger.exception("failed to write suggestion cursor")


def apply_suggestion(workspace: Path, action: dict) -> dict:
    """Replay an accepted action through the same apply functions curation uses.
    For a manual evolve, confirm=True (the user approved it in the bandeja)."""
    from durin.agent import skills_store as ss
    t = action.get("type")
    if t == "evolve":
        return ss.apply_skill_edit(
            workspace, action["name"], old=action["old"], new=action["new"],
            rationale=action.get("rationale", "evolve"),
            file=action.get("file", "SKILL.md"), confirm=True,
            attribution=ss.Attribution(actor="curation"))
    if t == "retire":
        return ss.remove_skill(workspace, action["name"])
    if t == "fuse":
        return ss.dream_fuse_skills(
            workspace, target=action["target"], content=action["content"],
            sources=action["sources"], rationale=action.get("rationale", "fuse"),
            attribution=ss.Attribution(actor="curation"))
    return {"error": f"unknown action type: {t}"}
