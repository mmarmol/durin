"""SkillsService — thin transport-agnostic wrapper over ``SkillsStore.web_*``.

Every service method maps 1:1 to a ``_handle_skill*`` handler in
``durin/channels/websocket.py``. The handler bodies are pure delegation to
``SkillsStore.web_*``; this service lifts that delegation out so the shim
becomes a thin auth + parse + serialize wrapper.

Result shape
------------
``SkillsStore.web_*`` returns ``(status: int, payload: dict[str, Any])``.
Rather than modelling every dynamic payload shape, all 18 methods share a
single ``SkillsResult`` that carries the raw ``(status, payload)`` tuple.
The shim reads ``result.status`` to forward the HTTP status code and passes
``result.data`` directly to ``_http_json_response``.  This is the
``dict[str, Any]`` escape hatch documented in the plan (open by design — the
skills store payload is dynamic).

The workspace must be injected at construction: ``SkillsService(workspace)``
because ``_endpoint_workspace()`` is a channel concern, not a service concern.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, Query, Result

# ---------------------------------------------------------------------------
# Shared result — all web_* calls return (status, payload)
# ---------------------------------------------------------------------------


class SkillsResult(Result):
    """Carries a store status code + raw payload dict (escape hatch)."""

    status: int
    data: dict[str, Any]


# ---------------------------------------------------------------------------
# Read DTOs
# ---------------------------------------------------------------------------


class SkillsListQuery(Query):
    """No inputs."""


class SkillsQuarantineQuery(Query):
    """No inputs."""


class SkillGetQuery(Query):
    name: str


class SkillFilesQuery(Query):
    name: str


class SkillFileQuery(Query):
    name: str
    path: str


class SkillHistoryQuery(Query):
    name: str


class SkillsResolveQuery(Query):
    source: str


class SkillSearchQuery(Query):
    q: str
    limit: int = 0


class SkillDescribeQuery(Query):
    ref: str


class GithubTokenTestQuery(Query):
    secret: str


class SkillJudgeQuery(Query):
    name: str


# ---------------------------------------------------------------------------
# Write DTOs
# ---------------------------------------------------------------------------


class SkillSaveCommand(Command):
    name: str
    content: str


class SkillFileSaveCommand(Command):
    name: str
    path: str
    content: str


class SkillModeCommand(Command):
    name: str
    value: str


class SkillsImportCommand(Command):
    source: str


class SkillApproveCommand(Command):
    name: str
    confirm: bool = False
    override: bool = False
    replace: bool = False
    install_deps: bool = False


class SkillInstallDepsCommand(Command):
    name: str
    bin_name: str | None = None


class SkillRejectCommand(Command):
    name: str


class SkillRemoveCommand(Command):
    name: str


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SkillsService:
    """Delegates all calls to ``SkillsStore.web_*`` after checking scope.

    ``workspace`` is the resolved gateway workspace (``Path``).  The shim
    injects it at construction from ``self._endpoint_workspace()``.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    # -- reads ---------------------------------------------------------------

    @route(
        "GET",
        "/api/v1/skills",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillsListQuery,
        response_model=SkillsResult,
        summary="List installed skills + store HEAD",
    )
    async def list(self, query: SkillsListQuery, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = ss.web_list(self._workspace)
        return SkillsResult(status=status, data=payload)

    @route(
        "GET",
        "/api/v1/skills/quarantine",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillsQuarantineQuery,
        response_model=SkillsResult,
        summary="List skills awaiting an import decision",
    )
    async def quarantine(self, query: SkillsQuarantineQuery, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = ss.web_quarantine(self._workspace)
        return SkillsResult(status=status, data=payload)

    @route(
        "GET",
        "/api/v1/skills/{name}",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillGetQuery,
        response_model=SkillsResult,
        summary="Fetch a skill's mode + SKILL.md content",
    )
    async def get(self, query: SkillGetQuery, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = ss.web_get(self._workspace, query.name)
        return SkillsResult(status=status, data=payload)

    @route(
        "GET",
        "/api/v1/skills/{name}/files",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillFilesQuery,
        response_model=SkillsResult,
        summary="List a skill's files",
    )
    async def files(self, query: SkillFilesQuery, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = ss.web_files(self._workspace, query.name)
        return SkillsResult(status=status, data=payload)

    @route(
        "GET",
        "/api/v1/skills/{name}/file",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillFileQuery,
        response_model=SkillsResult,
        summary="Read one skill file",
    )
    async def file_get(self, query: SkillFileQuery, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = ss.web_file_get(self._workspace, query.name, query.path)
        return SkillsResult(status=status, data=payload)

    @route(
        "GET",
        "/api/v1/skills/{name}/history",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillHistoryQuery,
        response_model=SkillsResult,
        summary="Provenance + attributed commit log",
    )
    async def history(self, query: SkillHistoryQuery, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = ss.web_history(self._workspace, query.name)
        return SkillsResult(status=status, data=payload)

    @route(
        "GET",
        "/api/v1/skills/resolve",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillsResolveQuery,
        response_model=SkillsResult,
        summary="List candidates a source points at",
    )
    async def resolve(self, query: SkillsResolveQuery, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = ss.web_import_resolve(self._workspace, query.source)
        return SkillsResult(status=status, data=payload)

    @route(
        "GET",
        "/api/v1/skills/search",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillSearchQuery,
        response_model=SkillsResult,
        summary="Search configured registries (async off-thread)",
    )
    async def search(self, query: SkillSearchQuery, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = await asyncio.to_thread(
            ss.web_skill_search, self._workspace, query.q, query.limit
        )
        return SkillsResult(status=status, data=payload)

    @route(
        "GET",
        "/api/v1/skills/describe",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillDescribeQuery,
        response_model=SkillsResult,
        summary="Lazy SKILL.md description peek (async off-thread)",
    )
    async def describe(self, query: SkillDescribeQuery, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = await asyncio.to_thread(ss.web_skill_describe, query.ref)
        return SkillsResult(status=status, data=payload)

    @route(
        "GET",
        "/api/v1/skills/github-token-test",
        scope=Scope.SKILLS_READ.value,
        request_model=GithubTokenTestQuery,
        response_model=SkillsResult,
        summary="Verify a GitHub-token secret",
    )
    async def github_token_test(
        self, query: GithubTokenTestQuery, principal: Principal
    ) -> SkillsResult:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = ss.web_github_token_test(query.secret)
        return SkillsResult(status=status, data=payload)

    @route(
        "GET",
        "/api/v1/skills/{name}/judge",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillJudgeQuery,
        response_model=SkillsResult,
        summary="Run the LLM judge on-demand (async off-thread)",
    )
    async def judge(self, query: SkillJudgeQuery, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = await asyncio.to_thread(
            ss.web_skill_judge, self._workspace, query.name
        )
        return SkillsResult(status=status, data=payload)

    # -- writes --------------------------------------------------------------

    @route(
        "POST",
        "/api/v1/skills/{name}/save",
        scope=Scope.SKILLS_WRITE.value,
        request_model=SkillSaveCommand,
        response_model=SkillsResult,
        summary="Overwrite a MANUAL skill",
    )
    async def save(self, cmd: SkillSaveCommand, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skills_store as ss

        status, payload = ss.web_save(self._workspace, cmd.name, cmd.content)
        return SkillsResult(status=status, data=payload)

    @route(
        "POST",
        "/api/v1/skills/{name}/file/save",
        scope=Scope.SKILLS_WRITE.value,
        request_model=SkillFileSaveCommand,
        response_model=SkillsResult,
        summary="Save one text file in a skill (manual)",
    )
    async def file_save(self, cmd: SkillFileSaveCommand, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skills_store as ss

        status, payload = ss.web_file_save(
            self._workspace,
            cmd.name,
            cmd.path,
            cmd.content,
            attribution=ss.Attribution(actor="user"),
        )
        return SkillsResult(status=status, data=payload)

    @route(
        "POST",
        "/api/v1/skills/{name}/mode",
        scope=Scope.SKILLS_WRITE.value,
        request_model=SkillModeCommand,
        response_model=SkillsResult,
        summary="Set a skill's mode (auto|manual)",
    )
    async def mode(self, cmd: SkillModeCommand, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skills_store as ss

        status, payload = ss.web_mode(self._workspace, cmd.name, cmd.value)
        return SkillsResult(status=status, data=payload)

    @route(
        "POST",
        "/api/v1/skills/import",
        scope=Scope.SKILLS_WRITE.value,
        request_model=SkillsImportCommand,
        response_model=SkillsResult,
        summary="Fetch one candidate to quarantine + scan",
    )
    async def import_skill(
        self, cmd: SkillsImportCommand, principal: Principal
    ) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skills_store as ss

        status, payload = ss.web_import_fetch(self._workspace, cmd.source)
        return SkillsResult(status=status, data=payload)

    @route(
        "POST",
        "/api/v1/skills/{name}/approve",
        scope=Scope.SKILLS_WRITE.value,
        request_model=SkillApproveCommand,
        response_model=SkillsResult,
        summary="Approve a quarantined skill",
    )
    async def approve(self, cmd: SkillApproveCommand, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skills_store as ss

        exec_run = ss._get_exec_run(self._workspace) if cmd.install_deps else None
        status, payload = await ss.web_skill_approve(
            self._workspace,
            cmd.name,
            confirm=cmd.confirm,
            override=cmd.override,
            replace=cmd.replace,
            install_deps=cmd.install_deps,
            exec_run=exec_run,
        )
        return SkillsResult(status=status, data=payload)

    @route(
        "POST",
        "/api/v1/skills/{name}/install-deps",
        scope=Scope.SKILLS_WRITE.value,
        request_model=SkillInstallDepsCommand,
        response_model=SkillsResult,
        summary="Install deps for a skill",
    )
    async def install_deps(
        self, cmd: SkillInstallDepsCommand, principal: Principal
    ) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skills_store as ss

        exec_run = ss._get_exec_run(self._workspace)
        status, payload = await ss.web_skill_install_deps(
            self._workspace, cmd.name, bin_name=cmd.bin_name, exec_run=exec_run
        )
        return SkillsResult(status=status, data=payload)

    @route(
        "DELETE",
        "/api/v1/skills/{name}/quarantine",
        scope=Scope.SKILLS_WRITE.value,
        request_model=SkillRejectCommand,
        response_model=SkillsResult,
        summary="Discard a quarantined skill",
    )
    async def reject(self, cmd: SkillRejectCommand, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skills_store as ss

        status, payload = ss.web_skill_reject(self._workspace, cmd.name)
        return SkillsResult(status=status, data=payload)

    @route(
        "DELETE",
        "/api/v1/skills/{name}",
        scope=Scope.SKILLS_WRITE.value,
        request_model=SkillRemoveCommand,
        response_model=SkillsResult,
        summary="Delete a workspace skill / revert a fork",
    )
    async def remove(self, cmd: SkillRemoveCommand, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skills_store as ss

        status, payload = ss.web_skill_remove(self._workspace, cmd.name)
        return SkillsResult(status=status, data=payload)
