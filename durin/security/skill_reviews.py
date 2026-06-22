"""Workspace-level "reviewed" overrides for active skills.

A user (or the LLM judge) can clear a flagged ACTIVE skill to a "Revisada"
state. The override is stored per-workspace (builtins live in the read-only
package dir, so the store must NOT be a sidecar in the skill dir). It is keyed
by a content hash AND the set of acked finding fingerprints: a review is valid
only while the content is unchanged AND no NEW finding appeared. Either a
content edit or a newly-detected finding (e.g. a scanner upgrade) re-opens it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock

_VERSION = 1


def _store_path(workspace) -> Path:
    return Path(workspace) / ".durin" / "skill-reviews.json"


def content_hash(skill_dir) -> str:
    """SHA-256 over the same surface scan_skill reads: SKILL.md + scripts/*."""
    skill_dir = Path(skill_dir)
    h = hashlib.sha256()
    md = skill_dir / "SKILL.md"
    if md.is_file():
        h.update(b"SKILL.md\0")
        h.update(md.read_bytes())
    scripts = skill_dir / "scripts"
    if scripts.is_dir():
        for p in sorted(scripts.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                h.update(p.relative_to(skill_dir).as_posix().encode("utf-8") + b"\0")
                h.update(p.read_bytes())
    return h.hexdigest()


def fingerprint(finding) -> str:
    if isinstance(finding, dict):
        return f"{finding['category']}|{finding['where']}|{finding['detail']}"
    return f"{finding.category}|{finding.where}|{finding.detail}"


def load_reviews(workspace) -> dict:
    p = _store_path(workspace)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — corrupt store must never break the inventory
        return {}
    reviews = data.get("reviews") if isinstance(data, dict) else None
    return reviews if isinstance(reviews, dict) else {}


def _write(workspace, reviews: dict) -> None:
    p = _store_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps({"version": _VERSION, "reviews": reviews}, indent=2))


def get_review(workspace, name, skill_dir, current_findings) -> dict | None:
    """Return the stored review for ``name`` only if it is still valid: the
    skill content is unchanged AND every current finding was already acked."""
    entry = load_reviews(workspace).get(name)
    if not isinstance(entry, dict):
        return None
    if entry.get("content_hash") != content_hash(skill_dir):
        return None
    acked = set(entry.get("acked") or [])
    if not {fingerprint(f) for f in current_findings}.issubset(acked):
        return None
    return entry


def record_review(workspace, name, skill_dir, *, by, verdict, original,
                  findings, note="") -> dict:
    """Record (or update) a review. The full load→mutate→save is under
    cross_process_lock so concurrent callers cannot lose each other's writes."""
    p = _store_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    with cross_process_lock(p):
        reviews = load_reviews(workspace)
        reviews[name] = {
            "content_hash": content_hash(skill_dir),
            "acked": sorted({fingerprint(f) for f in findings}),
            "by": by,
            "verdict": verdict,
            "original": original,
            "note": note or "",
            "at": date.today().isoformat(),
        }
        _write(workspace, reviews)
    return reviews[name]


def clear_review(workspace, name) -> bool:
    """Remove a review. The full load→mutate→save is under cross_process_lock."""
    p = _store_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    with cross_process_lock(p):
        reviews = load_reviews(workspace)
        if name not in reviews:
            return False
        del reviews[name]
        _write(workspace, reviews)
    return True
