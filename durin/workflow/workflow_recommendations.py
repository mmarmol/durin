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


def _validate_script_name(name: str) -> str | None:
    """Reject anything but a single relative path segment.

    Mirrors the rule the script editor's PUT door enforces (see
    ``_validate_script_name`` in the service layer): no '/', no '\\', no '..',
    no NUL. Reimplemented locally so workflow/ does not depend on service/.
    Returns an error message, or None when ``name`` is valid.
    """
    if not name or not name.strip():
        return "script name must not be empty"
    if name in (".", ".."):
        return f"script name {name!r} is not a valid filename"
    if "/" in name or "\\" in name or "\x00" in name:
        return f"script name {name!r} must be a single path segment (no '/')"
    return None


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
    manual_only: bool = False,
) -> str:
    """Record (or dedup-bump) a recommendation. Returns its stable id.

    ``manual_only`` flags a proposal that must never auto-apply even in
    ``improvement_mode: auto`` (e.g. an edit to a routing node) — it is stored
    only when True, so existing records stay shape-stable and readers must
    treat its absence as False.
    """
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
            record = {
                "id": rid, "workflow": name, "target_id": target_id, "field": field,
                "current": current, "proposed": proposed, "reason": reason,
                "status": "open", "count": 1, "run_ids": run_ids or [],
                "created_at": time.time(),
            }
            if manual_only:
                record["manual_only"] = True
            records.append(record)
        atomic_write_text(path, "\n".join(json.dumps(r) for r in records) + "\n")
    return rid


def open_recommendations(workspace: str | Path, name: str) -> list[dict]:
    """Recommendations awaiting the user's review (status == 'open')."""
    return [r for r in _read(_path(workspace, name)) if r.get("status") == "open"]


def log_script_file_recommendation(
    workspace: str | Path, name: str, *, script: str, current: str, proposed: str,
    reason: str, run_ids: list[str] | None = None, manual_only: bool = False,
) -> str:
    """Record (or dedup-bump) a script-file repair proposal. Returns its stable id.

    Unlike ``log_recommendation`` (which edits a node field through the workflow
    definition path), this targets a file under ``workflows/scripts/``: the
    record carries the full proposed file content so ``apply_recommendation``
    can write it atomically and snapshot it into the workflow version history.
    """
    rid = _rec_id("__script__:" + script, "script_file", proposed)
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
            record = {
                "id": rid, "workflow": name, "kind": "script_file", "script": script,
                "current": current, "proposed": proposed, "reason": reason,
                "status": "open", "count": 1, "run_ids": run_ids or [],
                "created_at": time.time(),
            }
            if manual_only:
                record["manual_only"] = True
            records.append(record)
        atomic_write_text(path, "\n".join(json.dumps(r) for r in records) + "\n")
    return rid


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

    For kinds ``command`` and ``script_file`` the deterministic pre-apply gate
    (``script_precheck.precheck_script_edit``) is re-run here too, even though
    a proposal's content never changes after it is recorded: the gate is
    spec-mandated at apply time as well, to catch environment drift between
    when the dream proposed the edit and when it (or a user) applies it. The
    precheck runs a multi-second syntax/security/smoke check via subprocess —
    never an LLM call — but it is still deliberately run OUTSIDE any lock so
    it never blocks a concurrent reader/writer of the recommendations file or
    the workflow definition. A precheck failure leaves the recommendation open
    and applies nothing.

    On success, the result also carries the pre-write value this call itself
    observed immediately before overwriting it (``previous`` for a definition
    field, ``previous_content`` for a script file, ``""`` if the file did not
    exist) — a caller that later needs a revert baseline (e.g. the dream's
    pending-validation marker) should use THIS value rather than one it read
    earlier, since an earlier read can be stale by the time this call's write
    actually lands (e.g. across this function's own precheck, or the model
    round-trip preceding it) and a stale baseline would misapply on revert.

    Returns ``{"ok": bool, ...}``."""
    import json as _json

    from durin.workflow.editing import save_workflow_definition
    from durin.workflow.loader import workflows_dir
    from durin.workflow.script_precheck import precheck_script_edit
    from durin.workflow.version_store import WorkflowVersionStore, version_lock_target

    path = _path(workspace, name)

    with cross_process_lock(path.with_suffix("")):
        records = _read(path)
        rec = next((r for r in records if r.get("id") == rec_id and r.get("status") == "open"), None)
        if rec is None:
            return {"ok": False, "error": f"no open recommendation {rec_id!r} for {name!r}"}
        if rec.get("kind") == "structural":
            return {"ok": False, "error": "a structural suggestion has no auto-apply — "
                                          "treat it in a session and edit the workflow deliberately"}
        kind = rec.get("kind")
        field = rec.get("field")
        script_name = rec.get("script")

    if kind == "script_file":
        name_error = _validate_script_name(script_name)
        if name_error:
            return {"ok": False, "error": name_error}
        ok, detail = precheck_script_edit("script_file", rec["proposed"], filename=script_name)
        if not ok:
            return {"ok": False, "error": f"precheck failed: {detail}"}
    elif field == "command":
        ok, detail = precheck_script_edit("command", rec["proposed"])
        if not ok:
            return {"ok": False, "error": f"precheck failed: {detail}"}

    # Re-acquire the lock to actually write. The record could have been
    # dismissed/applied/reverted by a concurrent caller while the precheck ran
    # above (it holds no lock) — re-fetching it here re-checks it is still open.
    with cross_process_lock(path.with_suffix("")):
        records = _read(path)
        rec = next((r for r in records if r.get("id") == rec_id and r.get("status") == "open"), None)
        if rec is None:
            return {"ok": False, "error": f"no open recommendation {rec_id!r} for {name!r}"}
        if rec.get("kind") == "script_file":
            script_name = rec["script"]
            script_path = workflows_dir(workspace) / "scripts" / script_name
            # Every writer of files under the versioned workflows dir serializes on
            # the version-lock target (the editor's script PUT, the definition save),
            # so a concurrent editor save and this apply never silently race. The
            # recommendations lock is already held; that nesting order matches the
            # definition-field path (which calls save_workflow_definition inside it).
            with cross_process_lock(version_lock_target(workflows_dir(workspace))):
                try:
                    previous_content = (script_path.read_text(encoding="utf-8")
                                        if script_path.is_file() else "")
                except OSError:
                    previous_content = ""
                try:
                    atomic_write_text(script_path, rec["proposed"])
                except OSError as exc:
                    return {"ok": False, "error": f"cannot write script {script_name!r}: {exc}"}
                # Best-effort: the scripts dir lives inside the versioned workflows dir,
                # so this snapshot lands the file edit in the same history as a
                # definition edit; versioning must never block the apply.
                commit = None
                try:
                    commit = WorkflowVersionStore(workflows_dir(workspace)).snapshot(
                        f"apply recommendation {rec_id}: {rec.get('reason', '')}"
                    )
                except Exception:  # noqa: BLE001 - versioning must not block the apply
                    commit = None
            rec["status"] = "applied"
            rec["applied_by"] = actor
            if commit:
                rec["applied_commit"] = commit
            atomic_write_text(path, "\n".join(_json.dumps(r) for r in records) + "\n")
            return {"ok": True, "script": script_name, "commit": commit,
                    "previous_content": previous_content}
        wf_path = workflows_dir(workspace) / f"{name}.json"
        try:
            data = _json.loads(wf_path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"cannot read workflow {name!r}: {exc}"}
        node = next((n for n in data.get("nodes", []) if n.get("id") == rec["target_id"]), None)
        if node is None:
            return {"ok": False, "error": f"node {rec['target_id']!r} no longer exists in {name!r}"}
        previous = node.get(rec["field"], "")
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
            "commit": saved.get("commit"), "previous": previous}
