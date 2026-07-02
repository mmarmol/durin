"""SkillsService — thin transport-agnostic wrapper over ``SkillsStore.web_*``.

Every service method maps 1:1 to a ``_handle_skill*`` handler in
``durin/channels/websocket.py``. The handler bodies are pure delegation to
``SkillsStore.web_*``; this service lifts that delegation out so the shim
becomes a thin auth + parse + serialize wrapper.

Result shape
------------
``SkillsStore.web_*`` returns ``(status: int, payload: dict[str, Any])``.
Rather than modelling every dynamic payload shape, ``_skills_result`` wraps a
2xx payload in a single ``SkillsResult`` (``data`` only) and raises the matching
DomainError for a non-2xx store status (payload echoed in ``details``) → the
adapter renders it as problem+json.  ``data`` is the ``dict[str, Any]`` escape
hatch documented in the plan (open by design — the skills store payload is dynamic).

The workspace must be injected at construction: ``SkillsService(workspace)``
because ``_endpoint_workspace()`` is a channel concern, not a service concern.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    ConflictError,
    DomainError,
    NotFoundError,
    Query,
    Result,
    ValidationFailedError,
)


def _emit(event: str, **data: Any) -> None:
    """Best-effort telemetry (never breaks the request)."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(event, data)
    except Exception:  # noqa: BLE001 — telemetry must never break the endpoint
        pass

# ---------------------------------------------------------------------------
# Shared result — all web_* calls return (status, payload)
# ---------------------------------------------------------------------------


class SkillsResult(Result):
    """A successful skills response — ``data`` is the raw store payload.

    The store returns ``(status, payload)``; a non-2xx status is raised as a
    DomainError by :func:`_skills_result` (payload echoed in ``details``) so the
    front door renders it as problem+json. Returned SkillsResults are 2xx only.
    """

    data: dict[str, Any]


def _skills_result(status: int, payload: dict[str, Any]) -> SkillsResult:
    """Map the store's ``(status, payload)`` to a 2xx ``SkillsResult`` or raise the
    matching DomainError (payload echoed in ``details``): 400 → validation (422),
    404 → not-found, 409 → conflict (the approval gate). Any other non-2xx is an
    unexpected store result → an internal error (500)."""
    if 200 <= status < 300:
        return SkillsResult(data=payload)
    message = str(
        payload.get("error") or payload.get("message") or f"skills request failed ({status})"
    )
    err_cls = {400: ValidationFailedError, 404: NotFoundError, 409: ConflictError}.get(status)
    if err_cls is None:
        raise DomainError(message, details=payload)  # unexpected store status → 500
    raise err_cls(message, details=payload)


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


class SkillCommitDiffQuery(Query):
    """One commit's diff, scoped to a skill's subtree."""

    name: str
    sha: str


class SkillCommitDiff(Result):
    """Unified diff of one commit for a skill."""

    sha: str
    patch: str


class SkillSuggestionsQuery(Query):
    """No inputs — returns the full pending-suggestion list."""


class SkillSuggestion(Result):
    """One curation suggestion for a manual skill, awaiting user review."""

    id: str
    skill: str
    type: str
    reason: str
    patch: str | None
    created_at: str


class SkillSuggestions(Result):
    """All pending skill suggestions."""

    suggestions: list[SkillSuggestion]


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
    # Re-import an already-installed skill, overwriting it. Without this the
    # import short-circuits with ``already_installed`` so the UI can offer it.
    replace: bool = False


class SkillApproveCommand(Command):
    name: str
    confirm: bool = False
    override: bool = False
    replace: bool = False
    install_deps: bool = False


class AcceptSuggestionCommand(Command):
    """Apply a suggestion (replays the curation action), then dequeue it."""

    id: str


class RejectSuggestionCommand(Command):
    """Reject a suggestion: write an expiring tombstone, then dequeue it."""

    id: str


class SkillInstallDepsCommand(Command):
    name: str
    bin_name: str | None = None


class SkillRejectCommand(Command):
    name: str


class SkillRemoveCommand(Command):
    name: str


class SkillReviewCommand(Command):
    name: str
    note: str = ""


class SkillUnreviewCommand(Command):
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
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

    @route(
        "GET",
        "/api/v1/skills/{name}/commit/{sha}/diff",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillCommitDiffQuery,
        response_model=SkillCommitDiff,
        summary="Unified diff of one commit, scoped to the skill",
    )
    async def commit_diff(
        self, query: SkillCommitDiffQuery, principal: Principal
    ) -> SkillCommitDiff:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skills_store as ss

        status, payload = ss.web_commit_diff(self._workspace, query.name, query.sha)
        if status != 200:
            raise NotFoundError(payload.get("error", "commit not found"))
        return SkillCommitDiff(sha=payload["sha"], patch=payload["patch"])

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

        # Off the event loop: resolving a source may do network (GitHub/HTTP
        # skill resolution) via a joined worker thread that would block the loop.
        status, payload = await asyncio.to_thread(
            ss.web_import_resolve, self._workspace, query.source,
        )
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

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

        # Off the event loop: this calls the GitHub API (network round-trip).
        status, payload = await asyncio.to_thread(ss.web_github_token_test, query.secret)
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

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

        # Off the event loop: web_save runs a blocking `bash -n` lint subprocess
        # (up to 10s) and a git commit; keeping it inline would stall the loop.
        status, payload = await asyncio.to_thread(
            ss.web_save, self._workspace, cmd.name, cmd.content,
        )
        return _skills_result(status, payload)

    @route(
        "POST",
        "/api/v1/skills/{name}/file/save",
        scope=Scope.SKILLS_WRITE.value,
        request_model=SkillFileSaveCommand,
        response_model=SkillsResult,
        summary="Save one text file in a skill",
    )
    async def file_save(self, cmd: SkillFileSaveCommand, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skills_store as ss

        # Off the event loop: web_file_save runs a blocking `bash -n` lint
        # subprocess (up to 10s for .sh files) and a git commit.
        status, payload = await asyncio.to_thread(
            ss.web_file_save,
            self._workspace,
            cmd.name,
            cmd.path,
            cmd.content,
            attribution=ss.Attribution(actor="user"),
        )
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

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

        status, payload = ss.web_import_fetch(self._workspace, cmd.source, replace=cmd.replace)
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

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
        return _skills_result(status, payload)

    @route(
        "GET",
        "/api/v1/skills/suggestions",
        scope=Scope.SKILLS_READ.value,
        request_model=SkillSuggestionsQuery,
        response_model=SkillSuggestions,
        summary="Pending curation suggestions for manual skills",
    )
    async def suggestions(
        self, query: SkillSuggestionsQuery, principal: Principal
    ) -> SkillSuggestions:
        principal.require(Scope.SKILLS_READ)
        from durin.agent import skill_suggestions as sg

        items = [
            SkillSuggestion(
                id=r["id"],
                skill=r.get("skill", ""),
                type=r.get("type", ""),
                reason=r.get("reason", ""),
                patch=r.get("patch"),
                created_at=r.get("created_at", ""),
            )
            for r in sg.read_suggestions(self._workspace)
        ]
        return SkillSuggestions(suggestions=items)

    @route(
        "POST",
        "/api/v1/skills/suggestions/{id}/accept",
        scope=Scope.SKILLS_WRITE.value,
        request_model=AcceptSuggestionCommand,
        response_model=SkillsResult,
        summary="Accept a skill suggestion (apply it)",
    )
    async def accept_suggestion(
        self, cmd: AcceptSuggestionCommand, principal: Principal
    ) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skill_suggestions as sg

        rec = sg.get_suggestion(self._workspace, cmd.id)
        if rec is None:
            raise NotFoundError(f"suggestion {cmd.id!r} not found")
        action = rec["action"]
        res = sg.apply_suggestion(self._workspace, action)
        if res.get("error"):
            raise ConflictError(str(res["error"]), details=res)
        sg.remove_suggestion(self._workspace, cmd.id)
        _emit("skill.suggestion_resolved",
             skill=action.get("name") or action.get("target", ""),
             action=action.get("type", ""), resolution="accepted")
        return _skills_result(200, {"ok": True})

    @route(
        "POST",
        "/api/v1/skills/suggestions/{id}/reject",
        scope=Scope.SKILLS_WRITE.value,
        request_model=RejectSuggestionCommand,
        response_model=SkillsResult,
        summary="Reject a skill suggestion (tombstone it)",
    )
    async def reject_suggestion(
        self, cmd: RejectSuggestionCommand, principal: Principal
    ) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skill_suggestions as sg

        rec = sg.get_suggestion(self._workspace, cmd.id)
        sg.add_tombstone(self._workspace, cmd.id)
        sg.remove_suggestion(self._workspace, cmd.id)
        if rec is not None:
            action = rec["action"]
            _emit("skill.suggestion_resolved",
                 skill=action.get("name") or action.get("target", ""),
                 action=action.get("type", ""), resolution="rejected")
        return _skills_result(200, {"ok": True})

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
        return _skills_result(status, payload)

    @route(
        "POST",
        "/api/v1/skills/{name}/review",
        scope=Scope.SKILLS_WRITE.value,
        request_model=SkillReviewCommand,
        response_model=SkillsResult,
        summary="Mark an active skill reviewed (user override to safe)",
    )
    async def review(self, cmd: SkillReviewCommand, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skills_store as ss

        status, payload = await asyncio.to_thread(
            ss.web_skill_review_user, self._workspace, cmd.name, cmd.note or ""
        )
        return _skills_result(status, payload)

    @route(
        "DELETE",
        "/api/v1/skills/{name}/review",
        scope=Scope.SKILLS_WRITE.value,
        request_model=SkillUnreviewCommand,
        response_model=SkillsResult,
        summary="Reopen an active skill review",
    )
    async def unreview(self, cmd: SkillUnreviewCommand, principal: Principal) -> SkillsResult:
        principal.require(Scope.SKILLS_WRITE)
        from durin.agent import skills_store as ss

        status, payload = await asyncio.to_thread(
            ss.web_skill_unreview, self._workspace, cmd.name
        )
        return _skills_result(status, payload)
