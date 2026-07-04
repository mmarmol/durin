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


def log_structural_suggestion(
    workspace: str | Path, name: str, *, proposal: dict, why_rejected: str,
    diagnostic: str, run_ids: list[str] | None = None,
) -> str:
    """Record a scope-REJECTED improvement idea for the user, annotated.

    The improve pass only ever applies/recommends prompt edits; when the model's
    proposal is structural (add/remove nodes, rewire, other fields), the idea is
    not silently dropped — it lands here with the full proposal, why the scope
    refused it, and the diagnostic evidence, so the user can open a session and
    treat it deliberately. NEVER auto-applied regardless of improvement_mode
    (``apply_recommendation`` refuses kind="structural")."""
    blob = json.dumps(proposal, sort_keys=True, ensure_ascii=False)
    rid = _rec_id("__structural__", "definition", blob)
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
                "id": rid, "workflow": name, "kind": "structural",
                "proposal": proposal, "why_rejected": why_rejected,
                "diagnostic": diagnostic, "reason": proposal.get("reason", ""),
                "status": "open", "count": 1, "run_ids": run_ids or [],
                "created_at": time.time(),
            })
        atomic_write_text(path, "\n".join(json.dumps(r) for r in records) + "\n")
    return rid


def dismiss_recommendation(workspace: str | Path, name: str, rec_id: str) -> bool:
    """User-dismiss an open recommendation (any kind). Terminal: the dedup id
    keeps a recurring identical proposal pinned to the dismissed record instead
    of re-queuing it."""
    path = _path(workspace, name)
    with cross_process_lock(path.with_suffix("")):
        records = _read(path)
        rec = next((r for r in records if r.get("id") == rec_id and r.get("status") == "open"), None)
        if rec is None:
            return False
        rec["status"] = "dismissed"
        atomic_write_text(path, "\n".join(json.dumps(r) for r in records) + "\n")
    return True


def mark_reverted(workspace: str | Path, name: str, rec_id: str, *, note: str) -> bool:
    """Mark an applied recommendation reverted (the auto-revert safety net).
    A reverted record never re-opens; log_recommendation dedup keeps a repeat
    proposal pinned to it instead of re-queuing."""
    path = _path(workspace, name)
    with cross_process_lock(path.with_suffix("")):
        records = _read(path)
        rec = next((r for r in records if r.get("id") == rec_id), None)
        if rec is None:
            return False
        rec["status"] = "reverted"
        rec["revert_note"] = note
        atomic_write_text(path, "\n".join(json.dumps(r) for r in records) + "\n")
    return True


def apply_recommendation(workspace: str | Path, name: str, rec_id: str,
                         *, actor: str = "user") -> dict:
    """Apply an open recommendation: write its proposed text into the workflow node's
    field through the shared editing engine (graph re-validated, atomic write,
    version commit with ``actor``), and mark it applied. The manual-mode apply path
    and the dream's auto mode both run through here.
    Returns ``{"ok": bool, ...}``."""
    import json as _json

    from durin.workflow.editing import save_workflow_definition
    from durin.workflow.loader import workflows_dir

    path = _path(workspace, name)
    with cross_process_lock(path.with_suffix("")):
        records = _read(path)
        rec = next((r for r in records if r.get("id") == rec_id and r.get("status") == "open"), None)
        if rec is None:
            return {"ok": False, "error": f"no open recommendation {rec_id!r} for {name!r}"}
        if rec.get("kind") == "structural":
            return {"ok": False, "error": "a structural suggestion has no auto-apply — "
                                          "treat it in a session and edit the workflow deliberately"}
        wf_path = workflows_dir(workspace) / f"{name}.json"
        try:
            data = _json.loads(wf_path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"cannot read workflow {name!r}: {exc}"}
        node = next((n for n in data.get("nodes", []) if n.get("id") == rec["target_id"]), None)
        if node is None:
            return {"ok": False, "error": f"node {rec['target_id']!r} no longer exists in {name!r}"}
        node[rec["field"]] = rec["proposed"]
        saved = save_workflow_definition(
            workspace, name, data,
            reason=f"apply recommendation {rec_id}: {rec.get('reason', '')}",
            actor=actor, must_exist=True,
        )
        if not saved.get("ok"):
            return {"ok": False, "error": saved.get("error", "save failed")}
        rec["status"] = "applied"
        rec["applied_commit"] = saved.get("commit")
        rec["applied_by"] = actor
        atomic_write_text(path, "\n".join(_json.dumps(r) for r in records) + "\n")
    return {"ok": True, "target_id": rec["target_id"], "field": rec["field"],
            "commit": saved.get("commit")}
