"""The single sanctioned write path for workflow definitions.

Every door that persists a workflow definition — the agent's ``workflow_write`` /
``workflow_edit`` tools, the dream improve pass, the recommendation apply step —
funnels through :func:`save_workflow_definition`: the graph is validated
(``parse_workflow``) before anything lands, the file is written atomically under
the same cross-process lock the HTTP editor uses, and the change is committed to
the workflow version store with its actor. The doors differ only in what they
may change (the agent edits full definitions on the user's behalf; the dream is
scoped to node prompts by the improve pass) — the safety plumbing is shared, so
no path can land an invalid graph or an unversioned edit.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock
from durin.workflow.loader import workflows_dir
from durin.workflow.spec import WorkflowError, parse_workflow
from durin.workflow.version_store import WorkflowVersionStore, version_lock_target


def safe_workflow_name(name: str) -> bool:
    """Reject names that could escape the workflows dir (path traversal)."""
    return bool(name) and name not in (".", "..") and not any(
        c in name for c in ("/", "\\", "\x00")
    )


def save_workflow_definition(
    workspace: str | Path,
    name: str,
    definition: dict,
    *,
    reason: str,
    actor: str,
    must_exist: bool,
) -> dict:
    """Validate and persist one workflow definition; returns ``{ok|error, ...}``.

    ``must_exist=False`` is the create door (refuses overwriting an existing
    name); ``must_exist=True`` is the edit door (refuses a missing name). The
    inner ``name`` field is forced consistent with the file name, and
    ``improvement_mode`` defaults to ``"manual"`` on create.
    """
    name = (name or "").strip()
    if not safe_workflow_name(name):
        return {"error": "invalid workflow name"}
    if not isinstance(definition, dict):
        return {"error": "definition must be a JSON object"}
    if not (reason or "").strip():
        return {"error": "rationale is required"}

    definition = dict(definition)
    definition["name"] = name                     # file name and inner name stay consistent
    if not must_exist:
        definition.setdefault("improvement_mode", "manual")
    try:
        parse_workflow(definition)
    except WorkflowError as exc:
        return {"error": f"invalid workflow: {exc}"}

    d = workflows_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.json"
    with cross_process_lock(version_lock_target(d)):
        if must_exist and not path.exists():
            return {"error": f"workflow does not exist: {name} (create it with workflow_write)"}
        if not must_exist and path.exists():
            return {"error": f"workflow already exists: {name}"}
        atomic_write_text(path, json.dumps(definition, indent=2, ensure_ascii=False))
    sha = None
    try:
        sha = WorkflowVersionStore(d).commit_edit(name, reason.strip(), actor=actor)
    except Exception as exc:  # noqa: BLE001 - versioning is best-effort, the write already landed
        logger.warning("workflow version commit failed for {}: {}", name, exc)
    return {"ok": True, "name": name, "commit": sha}
