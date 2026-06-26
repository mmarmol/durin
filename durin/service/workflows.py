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
from durin.workflow.spec import WorkflowError, parse_workflow
from durin.workflow.version_store import version_lock_target


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


class WorkflowDeleteCommand(Command):
    name: str


class WorkflowDeleteResult(Result):
    deleted: bool


class WorkflowRunCommand(Command):
    name: str
    task: str
    input_files: list[str] = []


class WorkflowRunResult(Result):
    status: str
    final_output: str
    run_id: str                       # the run's manifest id — the key for the read routes below
    runs: list[dict[str, Any]]        # per-node trace: node_id/iteration/passed/session_key/worker_index/status/route_label/output
    output_dir: str = ""
    exhausted_node: str = ""


class WorkflowRunManifestQuery(Query):
    name: str
    run_id: str


class WorkflowRunManifestResult(Result):
    manifest: dict[str, Any]   # the live/terminal run manifest (status, started/finished, per-node trace)


class WorkflowSessionRunsQuery(Query):
    session: str   # a root session key; lists the runs that session spawned


class WorkflowSessionRunsResult(Result):
    runs: list[dict[str, Any]]   # matching run manifests, newest-first


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
        names = [p.stem for p in d.glob("*.json") if p.is_file()] if d.is_dir() else []
        return WorkflowsListResult(workflows=sorted(names))

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
            parse_workflow(cmd.definition)   # reject an invalid graph before it lands
        except WorkflowError as exc:
            raise ValidationFailedError(f"invalid workflow: {exc}")
        path = self._dir() / f"{cmd.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with cross_process_lock(self._lock_target()):
            atomic_write_text(path, json.dumps(cmd.definition, indent=2, ensure_ascii=False))
        return WorkflowSaveResult(name=cmd.name)

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
        "POST", "/api/v1/workflows/{name}/run",
        scope=Scope.WORKFLOWS_WRITE.value,
        request_model=WorkflowRunCommand, response_model=WorkflowRunResult,
        summary="Run a workflow on a task (no live MCP — that path is the agent's).",
    )
    async def run(self, cmd: WorkflowRunCommand, principal: Principal) -> WorkflowRunResult:
        principal.require(Scope.WORKFLOWS_WRITE)
        if self._app_config is None or self._sessions is None:
            raise UnavailableError("running a workflow is not available on this surface")
        try:
            workflow = load_workflow(self._workspace, cmd.name)
        except WorkflowNotFound:
            raise NotFoundError(f"workflow {cmd.name!r} not found")

        import asyncio

        from durin.agent.runner import AgentRunner
        from durin.providers.factory import make_provider
        from durin.workflow.engine import WorkflowEngine
        from durin.workflow.judge import AgentJudgeRunner
        from durin.workflow.node_runner import AgentNodeRunner
        from durin.workflow.subworkflow import SubworkflowRunner

        preset = self._app_config.resolve_default_preset()
        provider = make_provider(self._app_config, preset=preset)
        runner = AgentRunner(provider)
        node_runner = AgentNodeRunner(
            runner, self._sessions, default_model=provider.get_default_model(),
            tools_config=self._app_config.tools,
            app_config=self._app_config,
        )
        judge = AgentJudgeRunner(runner, default_model=provider.get_default_model())
        ws = str(self._workspace)
        engine = WorkflowEngine(
            node_runner=node_runner,
            subworkflow_runner=SubworkflowRunner(ws, node_runner, judge),
            workspace=ws, pick_runner=judge.pick,
            max_node_visits=self._app_config.workflow.max_node_visits)
        result = await asyncio.to_thread(
            engine.run, workflow, cmd.task, input_files=cmd.input_files or None
        )
        # The engine owns the run manifest (started→updated→finalized); no record write here.
        return WorkflowRunResult(
            status=result.status,
            final_output=result.final_output or "",
            run_id=result.run_id,
            runs=[
                {"node_id": r.node_id, "iteration": r.iteration, "passed": r.passed,
                 "session_key": r.session_key, "worker_index": r.worker_index,
                 "status": r.status, "route_label": r.route_label,
                 "output": (r.output or "")[:2000]}
                for r in result.runs
            ],
            output_dir=result.output_dir or "",
            exhausted_node=result.exhausted_node or "",
        )

    @route(
        "GET", "/api/v1/workflows/runs",
        scope=Scope.WORKFLOWS_READ.value,
        request_model=WorkflowSessionRunsQuery, response_model=WorkflowSessionRunsResult,
        summary="List the run manifests a session spawned (forward lineage), newest-first.",
    )
    async def session_runs(self, query: WorkflowSessionRunsQuery, principal: Principal) -> WorkflowSessionRunsResult:
        principal.require(Scope.WORKFLOWS_READ)
        return WorkflowSessionRunsResult(runs=run_log.runs_for_session(self._workspace, query.session))

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
