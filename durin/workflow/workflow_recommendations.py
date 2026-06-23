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

from durin.utils.atomic_write import atomic_write_text
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
    with cross_process_lock(path.with_suffix("")):
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
        atomic_write_text(path, "\n".join(json.dumps(r) for r in records) + "\n")
    return rid


def open_recommendations(workspace: str | Path, name: str) -> list[dict]:
    """Recommendations awaiting the user's review (status == 'open')."""
    return [r for r in _read(_path(workspace, name)) if r.get("status") == "open"]


def apply_recommendation(workspace: str | Path, name: str, rec_id: str) -> dict:
    """Apply an open recommendation: write its proposed text into the workflow node's
    field, commit the edit (with the reason as a trailer), and mark it applied. This is
    the manual-mode apply path — and the same edit+commit step auto mode will reuse.
    Returns ``{"ok": bool, ...}``."""
    import json as _json

    from durin.workflow.loader import workflows_dir
    from durin.workflow.version_store import WorkflowVersionStore

    path = _path(workspace, name)
    with cross_process_lock(path.with_suffix("")):
        records = _read(path)
        rec = next((r for r in records if r.get("id") == rec_id and r.get("status") == "open"), None)
        if rec is None:
            return {"ok": False, "error": f"no open recommendation {rec_id!r} for {name!r}"}
        wf_path = workflows_dir(workspace) / f"{name}.json"
        try:
            data = _json.loads(wf_path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"cannot read workflow {name!r}: {exc}"}
        node = next((n for n in data.get("nodes", []) if n.get("id") == rec["target_id"]), None)
        if node is None:
            return {"ok": False, "error": f"node {rec['target_id']!r} no longer exists in {name!r}"}
        node[rec["field"]] = rec["proposed"]
        # Atomic write so a concurrent version snapshot (git add -A under its own lock)
        # can only ever see the whole old or whole new file, never a torn one.
        atomic_write_text(wf_path, _json.dumps(data, indent=2))
        WorkflowVersionStore(workflows_dir(workspace)).commit_edit(
            name, f"apply recommendation {rec_id}: {rec.get('reason', '')}"
        )
        rec["status"] = "applied"
        atomic_write_text(path, "\n".join(_json.dumps(r) for r in records) + "\n")
    return {"ok": True, "target_id": rec["target_id"], "field": rec["field"]}
