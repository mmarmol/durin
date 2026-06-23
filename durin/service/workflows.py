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
    ValidationFailedError,
)
from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock
from durin.workflow.loader import workflows_dir
from durin.workflow.spec import WorkflowError, parse_workflow


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


class WorkflowsService:
    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    def _dir(self) -> Path:
        return workflows_dir(self._workspace)

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
        with cross_process_lock(path):
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
        with cross_process_lock(path):
            path.unlink()
        return WorkflowDeleteResult(deleted=True)
