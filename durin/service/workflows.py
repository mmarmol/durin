"""WorkflowsService — list, load, save, and delete user workflow definitions.

Workflows live as JSON at ``<workspace>/workflows/<name>.json`` (see
``durin.workflow.loader``) and are validated by ``durin.workflow.spec.parse_workflow``.
This is the HTTP surface the webui visual editor uses to manage them. Saves are
validated before they land, and written atomically under the cross-process lock so a
concurrent version-store snapshot never sees a torn file.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    NotFoundError,
    Query,
    Result,
    UnavailableError,
    ValidationFailedError,
)
from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock
from durin.workflow import run_log
from durin.workflow.artifacts import safe_key
from durin.workflow.loader import WorkflowNotFound, load_workflow, workflows_dir
from durin.workflow.result import WorkflowResult
from durin.workflow.spec import WorkflowError, parse_workflow
from durin.workflow.version_store import WorkflowVersionStore, version_lock_target

# A script file's content cap for the PUT .../scripts/{name} editor route — generous
# for a deterministic node script, small enough to keep the JSON body and the atomic
# write cheap.
_MAX_SCRIPT_CONTENT_BYTES = 256 * 1024

# The websocket "chat" key the runs pane's live feed subscribes to. An
# agent-launched run publishes its progress on the calling session's own
# chat_id (see run_workflow.py); a service-path run — an automation trigger
# today, a raw HTTP launch tomorrow — has no calling chat to attach to,
# so every such run publishes onto this one fixed key instead.
RUNS_FEED_CHAT_ID = "runs:feed"


def build_runs_feed_event(payload: dict) -> dict:
    """Build the `workflow_progress` `tool_events` envelope from a service-path
    progress payload (`run_id`, `workflow`, `task`, `nodes`, `done`).

    Same six keys, same field names, as `durin/agent/tools/run_workflow.py`'s
    own per-chat progress publisher builds — so the existing work-panel
    renderer consumes a `runs:feed` frame exactly like a per-chat one, with no
    branching for "where did this come from". `done` is expressed the same
    way that publisher expresses it: via `phase` ("end" instead of a separate
    top-level flag), not carried as its own key.
    """
    return {
        "version": 1,
        "phase": "end" if payload.get("done") else "running",
        "call_id": f"workflow:{payload['run_id']}",
        "name": "workflow_progress",
        "arguments": {"workflow": payload["workflow"], "task": payload.get("task", "")},
        "nodes": payload["nodes"],
    }


def _validate_script_name(name: str) -> None:
    """Reject anything but a single relative path segment.

    Stricter than the workflow parser's script-path rule (which allows nested
    paths under ``workflows/scripts/``): the editor's create/edit door only ever
    writes a flat filename, so '/' (nesting, absolute paths on POSIX), '\\'
    (Windows-style nesting), and '..' are all rejected outright.
    """
    if not name or not name.strip():
        raise ValidationFailedError("script name must not be empty")
    if name in (".", ".."):
        raise ValidationFailedError(f"script name {name!r} is not a valid filename")
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValidationFailedError(f"script name {name!r} must be a single path segment (no '/')")


class WorkflowsListQuery(Query):
    """No inputs — lists every workflow name in the workspace."""


class WorkflowsListResult(Result):
    workflows: list[str]


class WorkflowGetQuery(Query):
    name: str


class WorkflowGetResult(Result):
    name: str
    definition: dict[str, Any]   # the raw on-disk JSON the editor renders and edits


class WorkflowSaveCommand(Command):
    name: str
    definition: dict[str, Any]


class WorkflowSaveResult(Result):
    name: str
    # Advisory only — the save succeeded. E.g. a node mode whose allowlist
    # references tools that never load in a workflow node.
    warnings: list[str] = []


class WorkflowDeleteCommand(Command):
    name: str


class WorkflowDeleteResult(Result):
    deleted: bool


class WorkflowDuplicateCommand(Command):
    name: str      # the source workflow to copy (path param)
    target: str    # the new workflow name (must not already exist)


class WorkflowDuplicateResult(Result):
    name: str      # the name of the created copy


class WorkflowRenameCommand(Command):
    name: str      # the workflow to rename (path param)
    target: str    # the new workflow name (must not already exist)


class WorkflowRenameResult(Result):
    name: str      # the workflow's new name


class WorkflowSeedSuggestionsResult(Result):
    # Each: {name, reason: "edited"|"unknown-provenance", created_at, diff}.
    # A suggestion is a builtin-template update the seeder could not apply
    # automatically because the workspace copy diverged from what was seeded.
    suggestions: list[dict[str, Any]]


class WorkflowSeedApplyCommand(Command):
    name: str


class WorkflowSeedApplyResult(Result):
    applied: bool
    error: str = ""


class WorkflowSeedDismissCommand(Command):
    name: str


class WorkflowSeedDismissResult(Result):
    dismissed: bool
    error: str = ""


class WorkflowRunCommand(Command):
    name: str
    task: str
    input_files: list[str] = []
    output_format: str = ""   # optional: how to deliver the result this run (overrides the workflow's output contract)
    resume_run_id: str = ""   # optional: resume a needs_input run of THIS workflow; task carries the user's answers


class WorkflowRunResult(Result):
    status: str
    final_output: str
    final_output_node: str = ""       # which node's output became final_output
    run_id: str                       # the run's manifest id — the key for the read routes below
    runs: list[dict[str, Any]]        # per-node trace: node_id/iteration/passed/session_key/worker_index/branch_id/budget/status/route_label/exit_code/command/stdout/stderr/model/provider/node_hash/origin_run_id/output
    output_dir: str = ""
    exhausted_node: str = ""
    needs_input_node: str = ""        # set when status=="needs_input": the node that asked
    output_files: list[str] = []      # relative paths in output_dir (completed runs)


class WorkflowLaunchCommand(Command):
    name: str      # the workflow to run (path param)
    task: str
    work_key: str = ""   # optional: a stable working folder shared by every launch with this key (see WorkflowEngine.run's work_key)


class WorkflowLaunchResult(Result):
    run_id: str


class WorkflowRunManifestQuery(Query):
    name: str
    run_id: str


class WorkflowRunManifestResult(Result):
    manifest: dict[str, Any]   # the live/terminal run manifest (status, started/finished, per-node trace)


class WorkflowSessionRunsQuery(Query):
    session: str = ""   # a root session key; lists the runs that session spawned.
                         # Omitted: the global feed across every workflow (the runs sidebar tab).
    limit: int = 50      # global-feed cap (ignored when `session` is set)


class WorkflowSessionRunsResult(Result):
    runs: list[dict[str, Any]]   # matching run manifests, newest-first


class WorkflowRunsListQuery(Query):
    name: str
    limit: int = 20


class WorkflowRunsListResult(Result):
    runs: list[dict[str, Any]]   # newest-first manifest summaries for this workflow


class WorkflowScriptsResult(Result):
    scripts: list[str]   # sorted filenames under <workspace>/workflows/scripts/, for script-node file pickers


class WorkflowScriptGetQuery(Query):
    name: str


class WorkflowScriptGetResult(Result):
    name: str
    content: str


class WorkflowScriptPutCommand(Command):
    name: str
    content: str


class WorkflowScriptPutResult(Result):
    name: str


class WorkflowRecsQuery(Query):
    name: str


class WorkflowRecsResult(Result):
    recommendations: list[dict[str, Any]]


class WorkflowRecApplyCommand(Command):
    name: str
    id: str


class WorkflowRecApplyResult(Result):
    ok: bool
    detail: str = ""


class WorkflowsService:
    def __init__(self, workspace: Path, *, app_config: Any = None, sessions: Any = None,
                 config_loader: Callable[[], Any] | None = None,
                 progress_publish: Callable[[dict], None] | None = None) -> None:
        self._workspace = Path(workspace)
        self._app_config = app_config   # for the run endpoint (provider); None on the catalog registry
        self._sessions = sessions       # SessionManager for node-session persistence during a run
        # The registry is wired once at gateway start; the operator can change the
        # default model afterwards and that write lands on disk. A run re-reads it
        # through this loader so it obeys the current default, not the wiring-time
        # snapshot. Left None where no config file backs the surface (tests/catalog).
        self._config_loader = config_loader
        # Optional live-progress sink for a service-path run (see RUNS_FEED_CHAT_ID
        # above). None (the default, and always on the catalog/wiring registrations)
        # means execute() builds the engine with no progress_emit at all — a run
        # that behaves exactly as before this existed.
        self._progress_publish = progress_publish
        # Strong-reference set for detached launch()es, so a run's task isn't
        # GC'd mid-flight — mirrors RunWorkflowTool's identical footgun guard.
        self._bg_tasks: set = set()

    def _live_config(self) -> Any:
        """The config this run must obey: re-read when a loader is wired, else the
        snapshot handed at construction."""
        if self._config_loader is None:
            return self._app_config
        return self._config_loader()

    def _dir(self) -> Path:
        return workflows_dir(self._workspace)

    def _scripts_dir(self) -> Path:
        return self._dir() / "scripts"

    def _lock_target(self) -> Path:
        # Lock beside the workflows dir on the same target the version store uses, so a
        # save/delete and a snapshot commit never interleave and no ".lock" artifact lands
        # inside the versioned dir.
        return version_lock_target(self._dir())

    def _repoint_automations(self, old: str, new: str) -> None:
        """Follow a rename into the automations that run this workflow.

        Sub-flow callers are repointed inline above because they live in the
        same directory and the same commit; an automation lives in its own
        directory with its own version store, so it needs its own save —
        `save_automation`, commit included. Without this a rename left every
        automation running the workflow pointing at a name that no longer
        resolves.

        Best-effort: the rename itself already landed, and an automation that
        cannot be rewritten must not turn a successful rename into an error.
        """
        from dataclasses import replace as _replace

        from durin.automations.store import load_automation, save_automation
        from durin.registry_graph import dependents_of

        for dep in dependents_of(self._workspace, workflow=old):
            if dep.kind != "automation":
                continue
            try:
                spec = load_automation(self._workspace, dep.name)
                save_automation(self._workspace, _replace(spec, workflow=new), actor="user",
                                 reason=f"workflow {old} was renamed to {new}")
            except Exception:  # noqa: BLE001
                logger.exception("could not repoint automation {} after renaming {}", dep.name, old)

    def _commit(self, paths: list[Path], subject: str, reason: str) -> None:
        """Record an editor mutation in the workflow version store.

        Every mutating route must call this: an edit that only changes the tree
        leaves no history and cannot be rolled back, and the periodic run
        snapshot would later sweep it up under an unrelated subject. Call it
        AFTER the write lock is released — the store takes that same
        cross-process lock, which is not reentrant.

        Best-effort by contract, like the rest of the store: the write already
        landed, so a versioning failure must never fail the request.
        """
        WorkflowVersionStore(self._dir()).commit_paths(paths, subject, reason, actor="user")

    @route(
        "GET", "/api/v1/workflows",
        scope=Scope.WORKFLOWS_READ.value,
        request_model=WorkflowsListQuery, response_model=WorkflowsListResult,
        summary="List all workflow names.",
    )
    async def list(self, query: WorkflowsListQuery, principal: Principal) -> WorkflowsListResult:
        principal.require(Scope.WORKFLOWS_READ)
        d = self._dir()
        # Dotfiles are seeding metadata (.seeds.json et al), not workflows.
        names = [p.stem for p in d.glob("*.json")
                 if p.is_file() and not p.name.startswith(".")] if d.is_dir() else []
        return WorkflowsListResult(workflows=sorted(names))

    @route(
        "GET", "/api/v1/workflows/scripts",
        scope=Scope.WORKFLOWS_READ.value,
        request_model=None, response_model=WorkflowScriptsResult,
        summary="List script filenames available to script nodes (the editor's file picker).",
    )
    async def list_scripts(self, principal: Principal) -> WorkflowScriptsResult:
        principal.require(Scope.WORKFLOWS_READ)
        d = self._workspace / "workflows" / "scripts"
        names = sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []
        return WorkflowScriptsResult(scripts=names)

    @route(
        "GET", "/api/v1/workflows/scripts/{name}",
        scope=Scope.WORKFLOWS_READ.value,
        request_model=WorkflowScriptGetQuery, response_model=WorkflowScriptGetResult,
        summary="Read one script file's content (the editor's script file viewer).",
    )
    async def get_script(self, query: WorkflowScriptGetQuery, principal: Principal) -> WorkflowScriptGetResult:
        principal.require(Scope.WORKFLOWS_READ)
        _validate_script_name(query.name)
        path = self._scripts_dir() / query.name
        if not path.is_file():
            raise NotFoundError(f"script {query.name!r} not found")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationFailedError(f"script {query.name!r} is unreadable: {exc}")
        return WorkflowScriptGetResult(name=query.name, content=content)

    # Static "seed-suggestions" paths win over the "/{name}" param routes via
    # build_api_app's _route_order sort (same guarantee /workflows/scripts uses).
    @route(
        "GET", "/api/v1/workflows/seed-suggestions",
        scope=Scope.WORKFLOWS_READ.value,
        request_model=None, response_model=WorkflowSeedSuggestionsResult,
        summary="Pending builtin-workflow seed updates (edited seeds the seeder will not overwrite).",
    )
    async def seed_suggestions(self, principal: Principal) -> WorkflowSeedSuggestionsResult:
        principal.require(Scope.WORKFLOWS_READ)
        from durin.workflow.seeds import list_suggestions

        return WorkflowSeedSuggestionsResult(
            suggestions=list_suggestions(self._workspace))

    @route(
        "POST", "/api/v1/workflows/seed-suggestions/apply",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowSeedApplyCommand, response_model=WorkflowSeedApplyResult,
        summary="Apply a pending seed update: overwrite the workflow with the new builtin template.",
    )
    async def seed_apply(self, cmd: WorkflowSeedApplyCommand, principal: Principal) -> WorkflowSeedApplyResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        from durin.workflow.seeds import apply_suggestion

        with cross_process_lock(self._lock_target()):
            out = apply_suggestion(self._workspace, cmd.name)
        return WorkflowSeedApplyResult(
            applied=bool(out.get("applied")), error=out.get("error", ""))

    @route(
        "POST", "/api/v1/workflows/seed-suggestions/dismiss",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowSeedDismissCommand, response_model=WorkflowSeedDismissResult,
        summary="Dismiss a pending seed update for this template version (a newer version will ask again).",
    )
    async def seed_dismiss(self, cmd: WorkflowSeedDismissCommand, principal: Principal) -> WorkflowSeedDismissResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        from durin.workflow.seeds import dismiss_suggestion

        out = dismiss_suggestion(self._workspace, cmd.name)
        return WorkflowSeedDismissResult(
            dismissed=bool(out.get("dismissed")), error=out.get("error", ""))

    @route(
        "PUT", "/api/v1/workflows/scripts/{name}",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowScriptPutCommand, response_model=WorkflowScriptPutResult,
        summary="Create or replace a script file (the editor's script create/edit action).",
    )
    async def put_script(self, cmd: WorkflowScriptPutCommand, principal: Principal) -> WorkflowScriptPutResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        _validate_script_name(cmd.name)
        # The shared editing engine validates, writes under the same lock and commits
        # to the workflow version history — the agent's workflow_script_write uses the
        # very same door, so neither surface can land an unversioned script.
        from durin.workflow.editing import save_workflow_script

        result = save_workflow_script(
            self._workspace, cmd.name, cmd.content,
            reason="saved in the workflow editor", actor="user",
        )
        if result.get("error"):
            raise ValidationFailedError(result["error"])
        return WorkflowScriptPutResult(name=cmd.name)

    @route(
        "GET", "/api/v1/workflows/{name}",
        scope=Scope.WORKFLOWS_READ.value,
        request_model=WorkflowGetQuery, response_model=WorkflowGetResult,
        summary="Load one workflow definition (the raw JSON).",
    )
    async def get(self, query: WorkflowGetQuery, principal: Principal) -> WorkflowGetResult:
        principal.require(Scope.WORKFLOWS_READ)
        path = self._dir() / f"{query.name}.json"
        if not path.is_file():
            raise NotFoundError(f"workflow {query.name!r} not found")
        try:
            definition = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationFailedError(f"workflow {query.name!r} is unreadable: {exc}")
        return WorkflowGetResult(name=query.name, definition=definition)

    @route(
        "POST", "/api/v1/workflows/{name}",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowSaveCommand, response_model=WorkflowSaveResult,
        summary="Create or update a workflow definition.",
    )
    async def save(self, cmd: WorkflowSaveCommand, principal: Principal) -> WorkflowSaveResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        try:
            parsed = parse_workflow(cmd.definition)   # reject an invalid graph before it lands
        except WorkflowError as exc:
            raise ValidationFailedError(f"invalid workflow: {exc}")
        from durin.workflow.editing import definition_warnings
        warnings = definition_warnings(parsed)
        path = self._dir() / f"{cmd.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        with cross_process_lock(self._lock_target()):
            atomic_write_text(path, json.dumps(cmd.definition, indent=2, ensure_ascii=False))
        self._commit(
            [path],
            f"workflow({cmd.name}): {'edit' if existed else 'create'}",
            "saved in the workflow editor",
        )
        return WorkflowSaveResult(name=cmd.name, warnings=warnings)

    @route(
        "DELETE", "/api/v1/workflows/{name}",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowDeleteCommand, response_model=WorkflowDeleteResult,
        summary="Delete a workflow definition.",
    )
    async def delete(self, cmd: WorkflowDeleteCommand, principal: Principal) -> WorkflowDeleteResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        path = self._dir() / f"{cmd.name}.json"
        if not path.is_file():
            raise NotFoundError(f"workflow {cmd.name!r} not found")
        # A sub-flow node and an automation both name a workflow by name, so
        # deleting one they point at breaks them silently. Refuse and say who
        # — the caller can retarget or remove the dependent first.
        from durin.registry_graph import dependents_of, describe

        deps = dependents_of(self._workspace, workflow=cmd.name)
        if deps:
            raise ValidationFailedError(
                f"workflow {cmd.name!r} is still used by {describe(deps)} — "
                f"retarget or remove those first"
            )
        with cross_process_lock(self._lock_target()):
            path.unlink()
        # Staging a path that no longer exists is what records the removal.
        self._commit([path], f"workflow({cmd.name}): delete", "deleted in the workflow editor")
        return WorkflowDeleteResult(deleted=True)

    @route(
        "POST", "/api/v1/workflows/{name}/duplicate",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowDuplicateCommand, response_model=WorkflowDuplicateResult,
        summary="Copy a workflow to a new name, to use as a starting point.",
    )
    async def duplicate(self, cmd: WorkflowDuplicateCommand, principal: Principal) -> WorkflowDuplicateResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        target = cmd.target.strip()
        if not target:
            raise ValidationFailedError("a duplicate needs a non-empty target name")
        src = self._dir() / f"{cmd.name}.json"
        if not src.is_file():
            raise NotFoundError(f"workflow {cmd.name!r} not found")
        try:
            definition = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationFailedError(f"workflow {cmd.name!r} is unreadable: {exc}")
        definition["name"] = target          # keep the inner name consistent with the file name
        parse_workflow(definition)            # the copy must still be a valid graph
        dest = self._dir() / f"{target}.json"
        with cross_process_lock(self._lock_target()):
            if dest.exists():
                raise ValidationFailedError(f"workflow {target!r} already exists")
            atomic_write_text(dest, json.dumps(definition, indent=2, ensure_ascii=False))
        self._commit(
            [dest],
            f"workflow({target}): create",
            f"duplicated from {cmd.name} in the workflow editor",
        )
        return WorkflowDuplicateResult(name=target)

    @route(
        "POST", "/api/v1/workflows/{name}/rename",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowRenameCommand, response_model=WorkflowRenameResult,
        summary="Rename a workflow: moves its definition, run history, and updates sub-flow references.",
    )
    async def rename(self, cmd: WorkflowRenameCommand, principal: Principal) -> WorkflowRenameResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        target = cmd.target.strip()
        if not target:
            raise ValidationFailedError("a rename needs a non-empty target name")
        src = self._dir() / f"{cmd.name}.json"
        if not src.is_file():
            raise NotFoundError(f"workflow {cmd.name!r} not found")
        try:
            definition = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationFailedError(f"workflow {cmd.name!r} is unreadable: {exc}")
        definition["name"] = target          # keep the inner name consistent with the file name
        parse_workflow(definition)            # the renamed graph must still be valid
        dest = self._dir() / f"{target}.json"
        # Every file this rename touches, so the whole move lands as ONE version:
        # a partial commit would leave a history where a caller points at a
        # workflow that does not exist.
        touched: list[Path] = [dest, src]
        with cross_process_lock(self._lock_target()):
            if dest.exists():
                raise ValidationFailedError(f"workflow {target!r} already exists")
            atomic_write_text(dest, json.dumps(definition, indent=2, ensure_ascii=False))
            src.unlink()
            # Repoint sub-flow nodes in every other definition so the rename does not
            # silently break callers.
            for sibling in self._dir().glob("*.json"):
                if sibling.name.startswith(".") or sibling == dest:
                    continue
                try:
                    sib_def = json.loads(sibling.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue                  # an unreadable sibling is not this rename's problem
                changed = False
                for node in sib_def.get("nodes", []):
                    if node.get("kind") == "subworkflow" and node.get("workflow") == cmd.name:
                        node["workflow"] = target
                        changed = True
                if changed:
                    atomic_write_text(sibling, json.dumps(sib_def, indent=2, ensure_ascii=False))
                    touched.append(sibling)
        self._commit(
            touched,
            f"workflow({cmd.name}): rename to {target}",
            "renamed in the workflow editor (definition, removal and caller references)",
        )
        self._repoint_automations(cmd.name, target)
        # Carry the run history (manifests, recommendations, dream cursor) to the new
        # name. Best-effort: a failure here never undoes the definition rename.
        src_runs = run_log.runs_root(self._workspace) / cmd.name
        dest_runs = run_log.runs_root(self._workspace) / target
        try:
            if src_runs.is_dir() and not dest_runs.exists():
                src_runs.rename(dest_runs)
                for manifest in dest_runs.glob("*.json"):
                    try:
                        data = json.loads(manifest.read_text(encoding="utf-8"))
                        if data.get("workflow") == cmd.name:
                            data["workflow"] = target
                            atomic_write_text(manifest, json.dumps(data, ensure_ascii=False))
                    except (OSError, json.JSONDecodeError):
                        continue
        except OSError:
            logger.exception("workflow rename could not move run history for {}", cmd.name)
        return WorkflowRenameResult(name=target)

    @route(
        "POST", "/api/v1/workflows/{name}/run",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowRunCommand, response_model=WorkflowRunResult,
        summary="Run a workflow on a task (no live MCP — that path is the agent's).",
    )
    async def run(self, cmd: WorkflowRunCommand, principal: Principal) -> WorkflowRunResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        result = await self.execute(
            cmd.name, cmd.task,
            input_files=cmd.input_files or None,
            output_format=cmd.output_format or None,
            resume_run_id=cmd.resume_run_id or None,
        )
        return WorkflowRunResult(
            status=result.status,
            final_output=result.final_output or "",
            final_output_node=result.final_output_node or "",
            run_id=result.run_id,
            runs=[
                {"node_id": r.node_id, "iteration": r.iteration, "passed": r.passed,
                 "session_key": r.session_key, "worker_index": r.worker_index,
                 "branch_id": r.branch_id, "budget": r.budget,
                 "status": r.status, "route_label": r.route_label,
                 "exit_code": getattr(r, "exit_code", None),
                 # Script nodes: the same record the manifest keeps, so a run
                 # opened straight from the editor reads like a stored one
                 # instead of showing an exit code with no output beside it.
                 "command": getattr(r, "command", None),
                 "stdout": getattr(r, "stdout", None),
                 "stderr": getattr(r, "stderr", None),
                 # Producer identity — same fields and getattr pattern as
                 # run_log._node_records(), so a run opened straight from this
                 # synchronous response carries the same producer trail (and, for
                 # a reused node, the same origin_run_id) a stored manifest does.
                 "model": getattr(r, "model", None),
                 "provider": getattr(r, "provider", None),
                 "node_hash": getattr(r, "node_hash", None),
                 "origin_run_id": getattr(r, "origin_run_id", None),
                 "output": (r.output or "")[:2000]}
                for r in result.runs
            ],
            output_dir=result.output_dir or "",
            exhausted_node=result.exhausted_node or "",
            needs_input_node=result.needs_input_node or "",
            output_files=list(result.output_files or []),
        )

    @route(
        "POST", "/api/v1/workflows/{name}/runs",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowLaunchCommand, response_model=WorkflowLaunchResult,
        status_code=202,
        summary="Launch a workflow run detached: returns immediately with the run id, never waits for it to finish.",
    )
    async def launch(self, cmd: WorkflowLaunchCommand, principal: Principal) -> WorkflowLaunchResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        try:
            load_workflow(self._workspace, cmd.name)
        except WorkflowNotFound:
            raise NotFoundError(f"workflow {cmd.name!r} not found")

        # Validate BEFORE detaching: an unsafe work_key must never reach the
        # background task below — by the time it raised there, this route
        # would already have returned 202 for a run that then fails invisibly
        # (logged, never reported to the caller). safe_key is the single
        # source of truth for what a valid key looks like; this just calls it
        # early instead of duplicating its rules. Empty means "no key" (see
        # `cmd.work_key or None` below) and is never validated — indistinguishable
        # from an omitted field on this plain-str command, exactly like every
        # other work_key surface's same falsy-is-omitted convention.
        if cmd.work_key:
            try:
                safe_key(cmd.work_key)
            except ValueError as exc:
                raise ValidationFailedError(f"invalid work_key: {exc}") from exc

        # Pre-generated so it can be returned before the engine ever starts —
        # same reason run_workflow's background branch does this (see
        # durin/agent/tools/run_workflow.py).
        run_id = uuid.uuid4().hex[:12]
        # No caller-supplied session for a raw API launch (unlike an agent
        # turn, which has one); key the run to the token that launched it,
        # the same way the /v1 chat endpoint keys a session to its caller.
        root_session_key = f"api:{principal.subject}"

        async def _run() -> None:
            try:
                await self.execute(
                    cmd.name, cmd.task, run_id=run_id, root_session_key=root_session_key,
                    work_key=cmd.work_key or None,
                )
            except Exception:
                logger.exception(
                    "detached run {} of workflow {!r} failed", run_id, cmd.name,
                )

        task = asyncio.create_task(_run())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return WorkflowLaunchResult(run_id=run_id)

    async def execute(
        self,
        name: str,
        task: str,
        *,
        input_files: list[str] | None = None,
        output_format: str | None = None,
        resume_run_id: str | None = None,
        run_id: str | None = None,
        root_session_key: str | None = None,
        work_key: str | None = None,
    ) -> WorkflowResult:
        if self._app_config is None or self._sessions is None:
            raise UnavailableError("running a workflow is not available on this surface")
        try:
            workflow = load_workflow(self._workspace, name)
        except WorkflowNotFound:
            raise NotFoundError(f"workflow {name!r} not found")

        # Same validation as launch()'s, but gated on `is not None` rather than
        # truthiness: unlike the HTTP command's plain str field, this parameter
        # already has a clean "no key" sentinel (None), so an explicitly empty
        # string here is a real (if invalid) key attempt — matching
        # WorkflowEngine.run's own is-not-None check for the identical value.
        # Raised here, before any provider/engine setup below, never as a bare
        # ValueError surfacing from inside asyncio.to_thread further down.
        if work_key is not None:
            try:
                safe_key(work_key)
            except ValueError as exc:
                raise ValidationFailedError(f"invalid work_key: {exc}") from exc

        from durin.agent.runner import AgentRunner
        from durin.providers.factory import make_provider
        from durin.workflow.engine import WorkflowEngine, build_resume_state
        from durin.workflow.judge import AgentJudgeRunner
        from durin.workflow.node_runner import AgentNodeRunner
        from durin.workflow.subworkflow import SubworkflowRunner

        resume = None
        if resume_run_id:
            manifest = run_log.read_manifest(self._workspace, name, resume_run_id)
            # A resume that names no root_session_key of its own (the common case
            # — the webui's answer/resume actions, an automation's resumed answer)
            # must not leave this None: WorkflowEngine.run's _start_manifest
            # re-stamps root_session_key on EVERY call, resume included, and reads
            # None as "synthesize a fresh workflow:<run_id>:root" — silently
            # overwriting whatever root the run's FIRST call recorded (e.g. an
            # automation's "automation:<name>" attribution) with a synthetic one.
            # Falling back to the manifest's own recorded value preserves it
            # through that re-stamp for every resume caller that doesn't pass one
            # explicitly.
            if root_session_key is None and manifest is not None:
                root_session_key = manifest.get("root_session_key")
            paused = (manifest is not None and manifest.get("status") == "needs_input"
                      and manifest.get("needs_input_node"))
            failed = (manifest is not None and manifest.get("status") == "aborted"
                      and manifest.get("failed_node"))
            if not paused and not failed:
                raise ValidationFailedError(
                    f"run {resume_run_id!r} of workflow {name!r} cannot be resumed — "
                    "only a needs_input run (with the answers as task) or an aborted "
                    "run (retried at its failed node) can."
                )
            if manifest.get("ask_kind") == "approval":
                from durin.workflow.approval import build_approval_resume, parse_approval_reply

                action = parse_approval_reply(task) or "revise"
                if action == "reject":
                    # No engine call at all: the approver declined it, which is not
                    # a failure — finalize 'cancelled' with rejected=True directly,
                    # IN PLACE on the existing manifest (preserves its per-node
                    # trace and work_dir; finalize_run would instead rewrite them
                    # away from this minimal result's empty runs=[]).
                    run_log.finalize_short_circuit(
                        self._workspace, name, resume_run_id,
                        status="cancelled", final_output=manifest.get("final_output"),
                        rejected=True,
                    )
                    return WorkflowResult(
                        status="cancelled", ask_kind=None,
                        final_output=manifest.get("final_output"),
                        run_id=resume_run_id, rejected=True,
                    )
                approval_resume = build_approval_resume(
                    workflow, manifest, action, task if action == "revise" else "")
                if approval_resume is None:
                    # Approve on a terminal approval node (no `next`): the run
                    # completes now, with the proposal as the final output — again
                    # no engine call, there is nowhere left for it to resume into.
                    run_log.finalize_short_circuit(
                        self._workspace, name, resume_run_id,
                        status="completed", final_output=manifest.get("final_output"),
                    )
                    return WorkflowResult(
                        status="completed", final_output=manifest.get("final_output"),
                        final_output_node=manifest.get("needs_input_node"),
                        run_id=resume_run_id,
                    )
                resume = approval_resume
            else:
                resume = build_resume_state(manifest, task)
            task = manifest.get("task") or task

        app_config = self._live_config()
        preset = app_config.resolve_default_preset()
        provider = make_provider(app_config, preset=preset)
        runner = AgentRunner(provider)
        node_runner = AgentNodeRunner(
            runner, self._sessions, default_model=provider.get_default_model(),
            tools_config=app_config.tools,
            app_config=app_config,
        )
        from durin.workflow.script_runner import ScriptNodeRunner
        script_runner = ScriptNodeRunner(
            self._workspace,
            default_timeout=app_config.workflow.script_timeout,
            max_output_chars=app_config.workflow.script_output_max_chars,
            log_max_chars=app_config.workflow.script_log_max_chars,
        )
        judge = AgentJudgeRunner(runner, default_model=provider.get_default_model())
        ws = str(self._workspace)
        wf_cfg = app_config.workflow

        # Pre-generate the run id (instead of letting the engine mint one
        # internally) so the cooperative cancel flags below — and the
        # tasks(action="stop") gate they back — are keyed by the SAME id the
        # engine actually uses. Mirrors run_workflow.py's identical
        # pre-generation and cancel_check/hard_cancel_check wiring (the
        # agent-launched path); without this, a run started through this
        # service (the HTTP run/launch routes, or a loop calling execute()
        # directly) ignored request_cancel() entirely.
        from durin.workflow.cancellation import clear as _clear_cancel
        from durin.workflow.cancellation import is_cancelled as _is_cancelled
        from durin.workflow.cancellation import is_hard_cancelled as _is_hard_cancelled
        rid = run_id or (resume.run_id if resume is not None else uuid.uuid4().hex[:12])

        # Wrap each engine frame as {run_id, workflow, task, nodes, done} for
        # the publisher — the SHAPE the engine hands progress_emit (see
        # WorkflowEngine's own progress_emit calls), just relabeled with this
        # run's workflow name and task so a caller with no other context (a
        # global feed, not a per-workflow one) can still tell runs apart —
        # `task` capped the same way a run manifest's own `task` field is
        # (durin/workflow/run_log.py). The engine never emits a terminal
        # (done=True) frame on its own; a caller that needs one builds it from
        # the returned WorkflowResult, same as run_workflow.py does.
        # `task or ""` guards a genuinely None task (e.g. an automation fired
        # with none) — WorkflowEngine calls progress_emit inside a bare
        # try/except that swallows any exception as best-effort, so a bare
        # `task[:200]` TypeError here would silently drop every frame for the
        # whole run instead of raising anywhere visible.
        progress_emit = None
        if self._progress_publish is not None:
            def _emit_progress(payload: dict) -> None:
                self._progress_publish({
                    "run_id": payload["run_id"],
                    "workflow": name,
                    "task": (task or "")[:200],
                    "nodes": payload["nodes"],
                    "done": False,
                })
            progress_emit = _emit_progress

        engine = WorkflowEngine(
            node_runner=node_runner,
            script_runner=script_runner,
            subworkflow_runner=SubworkflowRunner(
                ws, node_runner, judge, script_runner=script_runner,
                parallel_llm_concurrency=wf_cfg.parallel_llm_concurrency,
                parallel_script_concurrency=wf_cfg.parallel_script_concurrency),
            workspace=ws, pick_runner=judge.pick,
            max_node_visits=wf_cfg.max_node_visits,
            parallel_llm_concurrency=wf_cfg.parallel_llm_concurrency,
            parallel_script_concurrency=wf_cfg.parallel_script_concurrency,
            run_id_factory=lambda: rid,
            progress_emit=progress_emit,
            cancel_check=lambda: _is_cancelled(rid),
            hard_cancel_check=lambda: _is_hard_cancelled(rid),
            # Clears the registry entry BEFORE the terminal manifest write, not
            # after engine.run() returns — see WorkflowEngine.__init__'s on_run_end
            # docstring for why the ordering (not just the timing) is what a
            # caller polling the manifest directly (a status check,
            # tasks(action='stop')) needs: this makes "the manifest is terminal"
            # imply "the flag is already cleared", rather than merely usually true.
            on_run_end=_clear_cancel)
        try:
            result = await asyncio.to_thread(
                engine.run, workflow, task,
                root_session_key=root_session_key,
                input_files=input_files,
                output_format=output_format,
                resume=resume,
                work_key=work_key,
            )
        finally:
            # Idempotent backstop (clear() is a no-op if already absent): covers
            # a run that never reached _finalize_manifest at all (no workspace,
            # or a wiring error before the walk starts).
            _clear_cancel(rid)
        # The engine owns the run manifest (started→updated→finalized); no record write here.
        return result

    @route(
        "GET", "/api/v1/workflows/runs",
        scope=Scope.WORKFLOWS_READ.value,
        request_model=WorkflowSessionRunsQuery, response_model=WorkflowSessionRunsResult,
        summary="List a session's run manifests (forward lineage); without `session`, the global feed across every workflow, newest-first.",
    )
    async def session_runs(self, query: WorkflowSessionRunsQuery, principal: Principal) -> WorkflowSessionRunsResult:
        principal.require(Scope.WORKFLOWS_READ)
        if query.session:
            return WorkflowSessionRunsResult(runs=run_log.runs_for_session(self._workspace, query.session))
        return WorkflowSessionRunsResult(runs=run_log.list_all_runs(self._workspace, query.limit))

    @route(
        "GET", "/api/v1/workflows/{name}/runs",
        scope=Scope.WORKFLOWS_READ.value,
        request_model=WorkflowRunsListQuery, response_model=WorkflowRunsListResult,
        summary="List one workflow's persisted runs, newest-first.",
    )
    async def runs_list(self, query: WorkflowRunsListQuery, principal: Principal) -> WorkflowRunsListResult:
        principal.require(Scope.WORKFLOWS_READ)
        return WorkflowRunsListResult(runs=run_log.list_runs(self._workspace, query.name, query.limit))

    @route(
        "GET", "/api/v1/workflows/{name}/runs/{run_id}",
        scope=Scope.WORKFLOWS_READ.value,
        request_model=WorkflowRunManifestQuery, response_model=WorkflowRunManifestResult,
        summary="Read one run's manifest (status + per-node session trace).",
    )
    async def run_manifest(self, query: WorkflowRunManifestQuery, principal: Principal) -> WorkflowRunManifestResult:
        principal.require(Scope.WORKFLOWS_READ)
        manifest = run_log.read_manifest(self._workspace, query.name, query.run_id)
        if manifest is None:
            raise NotFoundError(f"run {query.run_id!r} of workflow {query.name!r} not found")
        return WorkflowRunManifestResult(manifest=manifest)

    @route(
        "GET", "/api/v1/workflows/{name}/recommendations",
        scope=Scope.WORKFLOWS_READ.value,
        request_model=WorkflowRecsQuery, response_model=WorkflowRecsResult,
        summary="List a workflow's open self-improvement recommendations.",
    )
    async def recommendations(self, query: WorkflowRecsQuery, principal: Principal) -> WorkflowRecsResult:
        principal.require(Scope.WORKFLOWS_READ)
        from durin.workflow.workflow_recommendations import open_recommendations
        return WorkflowRecsResult(recommendations=open_recommendations(self._workspace, query.name))

    @route(
        "POST", "/api/v1/workflows/{name}/recommendations/{id}/apply",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowRecApplyCommand, response_model=WorkflowRecApplyResult,
        summary="Apply a recommendation (writes its proposed edit into the workflow).",
    )
    async def apply_recommendation(self, cmd: WorkflowRecApplyCommand, principal: Principal) -> WorkflowRecApplyResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        from durin.workflow.workflow_recommendations import apply_recommendation as _apply
        res = _apply(self._workspace, cmd.name, cmd.id)
        return WorkflowRecApplyResult(ok=bool(res.get("ok")), detail=res.get("error", "") or res.get("field", ""))

    @route(
        "POST", "/api/v1/workflows/{name}/recommendations/{id}/dismiss",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowRecApplyCommand, response_model=WorkflowRecApplyResult,
        summary="Dismiss a recommendation (terminal; an identical repeat proposal stays pinned to it).",
    )
    async def dismiss_recommendation(self, cmd: WorkflowRecApplyCommand, principal: Principal) -> WorkflowRecApplyResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        from durin.workflow.workflow_recommendations import dismiss_recommendation as _dismiss
        ok = _dismiss(self._workspace, cmd.name, cmd.id)
        return WorkflowRecApplyResult(ok=ok, detail="" if ok else "no open recommendation with that id")
