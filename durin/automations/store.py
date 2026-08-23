"""Automation definition persistence: one JSON file per automation under
<workspace>/automations/.

Same model as workflow definitions: files are the truth, atomic full-file
rewrite; a per-automation cross-process lock guards save/delete because the
webui, the agent tool, and the CLI may write concurrently — writers touching
different automations never block each other, only same-name writers
serialize.

Saving validates the chain-trigger graph (durin.automations.chains) so no
save can close a cycle across automations. Deleting does not: removing a
chain source leaves any other automation's chain trigger pointing at a name
that no longer exists, but that dangling trigger is inert rather than
invalid — chain_targets simply never matches it — so v1 accepts it rather
than chasing down and rewriting every downstream chain trigger on delete.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from durin.automations.spec import AutomationNotFound, AutomationSpec, automation_to_dict, parse_automation
from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock


def automations_dir(workspace: str | Path) -> Path:
    return Path(workspace) / "automations"


def _path(workspace: str | Path, name: str) -> Path:
    return automations_dir(workspace) / f"{name}.json"


def load_automation(workspace: str | Path, name: str) -> AutomationSpec:
    p = _path(workspace, name)
    if not p.exists():
        raise AutomationNotFound(f"automation '{name}' not found")
    return parse_automation(json.loads(p.read_text(encoding="utf-8")))


def list_automations(workspace: str | Path) -> list[AutomationSpec]:
    d = automations_dir(workspace)
    if not d.is_dir():
        return []
    out: list[AutomationSpec] = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(parse_automation(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue  # malformed files are skipped, never fatal for listing
    return out


def save_automation(
    workspace: str | Path,
    spec: AutomationSpec,
    *,
    actor: str = "user",
    reason: str = "saved",
) -> None:
    from durin.automations.chains import validate_chain_edges

    validate_chain_edges(workspace, spec)
    d = automations_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    path = _path(workspace, spec.name)
    existed = path.exists()
    with cross_process_lock(automations_dir(workspace) / spec.name):
        atomic_write_text(path, json.dumps(automation_to_dict(spec), indent=2))
    _version(workspace, [path], f"automation({spec.name}): {'edit' if existed else 'create'}",
             reason, actor)


def delete_automation(
    workspace: str | Path,
    name: str,
    *,
    actor: str = "user",
    reason: str = "deleted",
) -> None:
    p = _path(workspace, name)
    with cross_process_lock(automations_dir(workspace) / name):
        if not p.exists():
            raise AutomationNotFound(f"automation '{name}' not found")
        p.unlink()
    _version(workspace, [p], f"automation({name}): delete", reason, actor)


def _version(
    workspace: str | Path, paths: list[Path], subject: str, reason: str, actor: str,
) -> None:
    """Record the change in the automation version store.

    Lives here rather than in each caller so every door — the webui service,
    the agent tool, the CLI — is versioned structurally instead of by
    remembering to ask. Best-effort by contract: the definition already
    landed, so a git failure must not turn a successful save into an error.
    """
    from durin.automations.version_store import AutomationVersionStore

    try:
        AutomationVersionStore(automations_dir(workspace)).commit_paths(
            paths, subject, reason, actor=actor)
    except Exception:  # noqa: BLE001
        logger.warning("automation version commit failed for {}", subject)
