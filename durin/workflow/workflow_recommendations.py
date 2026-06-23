"""The recommendation queue for manual-mode workflow self-improvement.

dream proposes an edit (to a node prompt or a gate criterion) and records it here for
the user to review and apply — manual mode never auto-edits. Stored as a per-workflow
JSONL beside the run records (``workflows-runs/<name>/.recommendations.jsonl``), mirroring
the skill_observations queue. A proposal that recurs across nightly runs is deduplicated
(same target + field + normalized text) and its count/run-ids are merged rather than
piling up duplicates.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from durin.utils.file_lock import cross_process_lock
from durin.workflow.run_log import runs_root


def _path(workspace: str | Path, name: str) -> Path:
    return runs_root(workspace) / name / ".recommendations.jsonl"


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _rec_id(target_id: str, field: str, proposed: str) -> str:
    key = f"{target_id}\x00{field}\x00{_norm(proposed)}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:12]


def _read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def log_recommendation(
    workspace: str | Path, name: str, *, target_id: str, field: str,
    current: str, proposed: str, reason: str, run_ids: list[str] | None = None,
) -> str:
    """Record (or dedup-bump) a recommendation. Returns its stable id."""
    rid = _rec_id(target_id, field, proposed)
    path = _path(workspace, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with cross_process_lock(path.with_suffix(".lock")):
        records = _read(path)
        existing = next((r for r in records if r.get("id") == rid), None)
        if existing is not None:
            existing["count"] = existing.get("count", 1) + 1
            merged = list(dict.fromkeys([*existing.get("run_ids", []), *(run_ids or [])]))
            existing["run_ids"] = merged
        else:
            records.append({
                "id": rid, "workflow": name, "target_id": target_id, "field": field,
                "current": current, "proposed": proposed, "reason": reason,
                "status": "open", "count": 1, "run_ids": run_ids or [],
                "created_at": time.time(),
            })
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return rid


def open_recommendations(workspace: str | Path, name: str) -> list[dict]:
    """Recommendations awaiting the user's review (status == 'open')."""
    return [r for r in _read(_path(workspace, name)) if r.get("status") == "open"]
