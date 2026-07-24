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


# A script file's content cap — generous for a deterministic node script, small
# enough to keep an HTTP JSON body and the atomic write cheap.
MAX_SCRIPT_CONTENT_BYTES = 256 * 1024


def save_workflow_script(
    workspace: str | Path,
    name: str,
    content: str,
    *,
    reason: str,
    actor: str,
) -> dict:
    """Validate and persist one workflow script file; returns ``{ok|error, ...}``.

    The script sibling of :func:`save_workflow_definition`, and for the same
    reason: ``workflows/scripts/`` holds the code that script nodes execute, so
    it needs one door that validates, writes under the shared lock and commits —
    not a generic file write that leaves the version store dirty.

    The name must be a single relative path segment. That is stricter than the
    workflow parser's script-path rule (which allows nesting), matching what the
    editor's create/edit door has always written: a flat filename.
    """
    name = (name or "").strip()
    if not name or name in (".", "..") or any(c in name for c in ("/", "\\", "\x00")):
        return {"error": f"invalid script name: {name!r} must be a single path segment"}
    if len(content.encode("utf-8")) > MAX_SCRIPT_CONTENT_BYTES:
        return {"error": f"script content exceeds the {MAX_SCRIPT_CONTENT_BYTES}-byte cap"}
    if not (reason or "").strip():
        return {"error": "rationale is required"}

    d = workflows_dir(workspace)
    scripts = d / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / name
    existed = path.exists()
    with cross_process_lock(version_lock_target(d)):
        atomic_write_text(path, content)
    sha = None
    try:
        sha = WorkflowVersionStore(d).commit_paths(
            [path], f"script({name}): {'edit' if existed else 'create'}",
            reason.strip(), actor=actor,
        )
    except Exception as exc:  # noqa: BLE001 - versioning is best-effort, the write landed
        logger.warning("workflow script version commit failed for {}: {}", name, exc)
    return {"ok": True, "name": name, "commit": sha}


_background_names_cache: frozenset[str] | None = None


def _background_tool_names() -> frozenset[str]:
    """Names of the built-in tools that can load in a background (subagent-scope)
    registry — the surface a workflow node's mode allowlist filters.

    Built with dummy aux handles so the vision/audio bridges count as
    background-capable regardless of this deployment's aux-model config: the
    question here is "can this entry EVER apply to a node", not "will it load
    today". Cached for the process lifetime — the built-in surface is static.
    """
    global _background_names_cache
    if _background_names_cache is None:
        try:
            from durin.agent.tools.context import AuxProviderHandle, ToolContext
            from durin.agent.tools.loader import ToolLoader
            from durin.agent.tools.registry import ToolRegistry
            from durin.config.schema import ToolsConfig

            handle = AuxProviderHandle(provider=None, model="")
            ctx = ToolContext(
                config=ToolsConfig(),
                workspace="/",
                scope="subagent",
                aux_providers={"vision": handle, "audio": handle},
            )
            registry = ToolRegistry()
            ToolLoader().load(ctx, registry, scope="subagent")
            _background_names_cache = frozenset(registry.tool_names)
        except Exception:  # noqa: BLE001 - warnings are advisory; never block a save
            _background_names_cache = frozenset()
    return _background_names_cache


_MAX_WARNED_ENTRIES = 5


def definition_warnings(workflow: object) -> list[str]:
    """Advisory warnings for a parsed workflow — things the parser accepts but
    that will not behave the way the author likely expects at run time.

    - A node ``mode`` that is not a registered mode name: ``get_mode`` silently
      falls back to ``build`` (FULL tool access), so a typo'd custom mode
      *widens* the node's surface instead of narrowing it.
    - Mode-allowlist entries that can never apply to a node because the tool is
      not background-capable (not ``subagent``-scoped). ``mcp_*`` entries are
      skipped — MCP availability is decided by the node's ``mcps`` field, not
      by scope.
    """
    from durin.agent.agent_mode import get_mode, list_modes

    warnings: list[str] = []
    known = {m.name for m in list_modes()}
    background = _background_tool_names()
    for node in getattr(workflow, "nodes", {}).values():
        if getattr(node, "kind", "") != "work":
            continue
        mode_name = getattr(node, "mode", "build")
        if mode_name not in known:
            warnings.append(
                f"node {node.id!r}: mode {mode_name!r} is not a registered mode — "
                "at run time it silently falls back to 'build' (full tool access)"
            )
            continue
        mode = get_mode(mode_name)
        if mode.allowed is None or not background:
            continue
        dead = sorted(
            n for n in mode.allowed if n not in background and not n.startswith("mcp_")
        )
        if dead:
            shown = ", ".join(dead[:_MAX_WARNED_ENTRIES])
            if len(dead) > _MAX_WARNED_ENTRIES:
                shown += f", … ({len(dead) - _MAX_WARNED_ENTRIES} more)"
            warnings.append(
                f"node {node.id!r}: mode {mode_name!r} allows tools that never load "
                f"in a workflow node (main-agent only): {shown}"
            )
    return warnings


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
        parsed = parse_workflow(definition)
    except WorkflowError as exc:
        return {"error": f"invalid workflow: {exc}"}
    warnings = definition_warnings(parsed)

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
    out: dict = {"ok": True, "name": name, "commit": sha}
    if warnings:
        out["warnings"] = warnings
    return out
