"""WorkflowsService — list, load, save, and delete user workflow definitions.

Workflows live as JSON at ``<workspace>/workflows/<name>.json`` (see
``durin.workflow.loader``) and are validated by ``durin.workflow.spec.parse_workflow``.
This is the HTTP surface the webui visual editor uses to manage them. Saves are
validated before they land, and written atomically under the cross-process lock so a
concurrent version-store snapshot never sees a torn file.
"""

from __future__ import annotations

import json
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
from durin.workflow.loader import WorkflowNotFound, load_workflow, workflows_dir
from durin.workflow.result import WorkflowResult
from durin.workflow.spec import WorkflowError, parse_workflow
from durin.workflow.version_store import WorkflowVersionStore, version_lock_target

# A script file's content cap for the PUT .../scripts/{name} editor route — generous
# for a deterministic node script, small enough to keep the JSON body and the atomic
# write cheap.
_MAX_SCRIPT_CONTENT_BYTES = 256 * 1024


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
    runs: list[dict[str, Any]]        # per-node trace: node_id/iteration/passed/session_key/worker_index/branch_id/budget/status/route_label/exit_code/output
    output_dir: str = ""
    exhausted_node: str = ""
    needs_input_node: str = ""        # set when status=="needs_input": the node that asked
    output_files: list[str] = []      # relative paths in output_dir (completed runs)


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
    def __init__(self, workspace: Path, *, app_config: Any = None, sessions: Any = None) -> None:
        self._workspace = Path(workspace)
        self._app_config = app_config   # for the run endpoint (provider); None on the catalog registry
        self._sessions = sessions       # SessionManager for node-session persistence during a run

    def _dir(self) -> Path:
        return workflows_dir(self._workspace)

    def _scripts_dir(self) -> Path:
        return self._dir() / "scripts"

    def _lock_target(self) -> Path:
        # Lock beside the workflows dir on the same target the version store uses, so a
        # save/delete and a snapshot commit never interleave and no ".lock" artifact lands
        # inside the versioned dir.
        return version_lock_target(self._dir())

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
        if len(cmd.content.encode("utf-8")) > _MAX_SCRIPT_CONTENT_BYTES:
            raise ValidationFailedError(
                f"script content exceeds the {_MAX_SCRIPT_CONTENT_BYTES}-byte cap"
            )
        d = self._scripts_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / cmd.name
        with cross_process_lock(self._lock_target()):
            atomic_write_text(path, cmd.content)
        # Snapshot into the workflow version history: scripts/ lives inside the
        # git-versioned workflows dir, so a script edit lands in the same history as a
        # workflow definition edit. Best-effort: never blocks the write above.
        WorkflowVersionStore(self._dir()).snapshot(f"script {cmd.name}")
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
        with cross_process_lock(self._lock_target()):
            atomic_write_text(path, json.dumps(cmd.definition, indent=2, ensure_ascii=False))
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
        with cross_process_lock(self._lock_target()):
            path.unlink()
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
                 "output": (r.output or "")[:2000]}
                for r in result.runs
            ],
            output_dir=result.output_dir or "",
            exhausted_node=result.exhausted_node or "",
            needs_input_node=result.needs_input_node or "",
            output_files=list(result.output_files or []),
        )

    async def execute(
        self,
        name: str,
        task: str,
        *,
        input_files: list[str] | None = None,
        output_format: str | None = None,
        resume_run_id: str | None = None,
    ) -> WorkflowResult:
        if self._app_config is None or self._sessions is None:
            raise UnavailableError("running a workflow is not available on this surface")
        try:
            workflow = load_workflow(self._workspace, name)
        except WorkflowNotFound:
            raise NotFoundError(f"workflow {name!r} not found")

        import asyncio

        from durin.agent.runner import AgentRunner
        from durin.providers.factory import make_provider
        from durin.workflow.engine import WorkflowEngine, build_resume_state
        from durin.workflow.judge import AgentJudgeRunner
        from durin.workflow.node_runner import AgentNodeRunner
        from durin.workflow.subworkflow import SubworkflowRunner

        resume = None
        if resume_run_id:
            manifest = run_log.read_manifest(self._workspace, name, resume_run_id)
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
            resume = build_resume_state(manifest, task)
            task = manifest.get("task") or task

        preset = self._app_config.resolve_default_preset()
        provider = make_provider(self._app_config, preset=preset)
        runner = AgentRunner(provider)
        node_runner = AgentNodeRunner(
            runner, self._sessions, default_model=provider.get_default_model(),
            tools_config=self._app_config.tools,
            app_config=self._app_config,
        )
        from durin.workflow.script_runner import ScriptNodeRunner
        script_runner = ScriptNodeRunner(
            self._workspace,
            default_timeout=self._app_config.workflow.script_timeout,
            max_output_chars=self._app_config.workflow.script_output_max_chars,
        )
        judge = AgentJudgeRunner(runner, default_model=provider.get_default_model())
        ws = str(self._workspace)
        wf_cfg = self._app_config.workflow
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
            parallel_script_concurrency=wf_cfg.parallel_script_concurrency)
        result = await asyncio.to_thread(
            engine.run, workflow, task,
            input_files=input_files,
            output_format=output_format,
            resume=resume,
        )
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
