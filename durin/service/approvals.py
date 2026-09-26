"""ApprovalsService — a person decides a pending approval request over HTTP.

``POST /api/v1/approvals/{id}/decision`` is how the dashboard's Pending page
approves or rejects a request filed in ``<workspace>/.approvals/``. It hands
the decision to ``durin.agent.approval.decide`` with the gateway's live
handles (``AgentLoop.approval_exec_deps``) and never re-implements its rules:
an exec request is refused outside the turn that asked, a kind whose runner is
missing here is refused before the record moves, a request past its TTL is
expired instead of decided, and a verdict for a turn still waiting on it is
handed to that turn.

What this service adds:

* **Who may decide.** Only the dashboard session a person signed in to
  (a ``webui`` principal). A token issued for the API, the static token and
  in-process callers — the agent's own tools among them — are refused: input
  from a program carries no authority to approve. On top of that, the change's
  domain scope: ``skills:write`` for a skill kind, ``mcp:write`` for an MCP
  change, ``admin`` for anything else (a shell command, a legacy record).
* **How an outcome maps to HTTP.** 200 whenever the request was acted on —
  ``applied``, ``rejected``, ``pending`` (handed to the waiting turn),
  ``failed`` (approved, but running it failed) and ``stale`` (it expired, or
  what it would change changed since it was reviewed) — with ``{status,
  message, approval_id}``. 409 when ``decide`` refused and left the record as
  it was (``refused``: already decided, a legacy record, a kind that cannot
  run here), with the same three fields in the problem's ``details``.
* **The chat that asked hears the outcome.** A decision that did not go to a
  waiting turn posts a system note into the chat session that filed the
  request (``durin.agent.approval_notify``).

A decision can run an executor for minutes (an MCP install, a dependency
install), so it runs as its own task that the request only awaits: a client
that disconnects or times out does not cut it off halfway.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import Field

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    Result,
)


def decider_of(principal: Principal) -> dict:
    """Who a decision made through a service route is recorded as, from the
    principal that made it: the person for a dashboard session
    (``{"kind": "user", "channel": "webui"}``), and otherwise an operator,
    named the way ``durin approvals`` names its CLI operator: a token
    (``{"kind": "operator", "channel": "api", "principal": <token id>}``, the
    static token's id being ``"static"``) or an in-process caller
    (``{"kind": "operator", "channel": "local"}``)."""
    if principal.kind == "webui":
        return {"kind": "user", "channel": "webui"}
    if principal.kind == "local":
        return {"kind": "operator", "channel": "local"}
    return {"kind": "operator", "channel": "api", "principal": principal.subject}


# The scope a decision on each kind takes: the domain the change lands in.
# Reading a request (the Pending list) takes the matching read scope. A kind
# with no domain of its own — a shell command, a legacy record — takes admin.
_DECISION_SCOPES: dict[str, Scope] = {
    "skill_install": Scope.SKILLS_WRITE,
    "skill_edit": Scope.SKILLS_WRITE,
    "skill_deps": Scope.SKILLS_WRITE,
    "mcp_change": Scope.MCP_WRITE,
}
_READ_SCOPES: dict[str, Scope] = {
    "skill_install": Scope.SKILLS_READ,
    "skill_edit": Scope.SKILLS_READ,
    "skill_deps": Scope.SKILLS_READ,
    "mcp_change": Scope.MCP_READ,
}


def decision_scope(kind: str | None) -> Scope:
    """The scope deciding a request of *kind* takes."""
    return _DECISION_SCOPES.get(kind or "", Scope.ADMIN)


def read_scope(kind: str | None) -> Scope:
    """The scope seeing a request of *kind* takes."""
    return _READ_SCOPES.get(kind or "", Scope.ADMIN)


class ApprovalDecisionCommand(Command):
    # Record ids are minted as 12 hex characters. Anything else is refused
    # before it can name a path under ``.approvals/``.
    id: str = Field(pattern=r"^[0-9a-f]{12}$")
    decision: Literal["approve", "reject"]


class ApprovalDecisionResult(Result):
    # applied | rejected | pending (handed to the waiting turn) | failed | stale
    status: str
    message: str
    approval_id: str


class ApprovalsService:
    """Decide pending approval requests on behalf of a person.

    ``exec_deps`` returns the live handles an approved request runs with
    (``AgentLoop.approval_exec_deps``); without it a decision runs with none,
    and a kind that needs one is refused. ``bus`` and ``serves_channel`` post
    the note to the chat that asked; ``serves_channel(name)`` says whether
    this process serves that channel, and without it no note is posted.
    """

    def __init__(
        self,
        workspace_resolver: Callable[[], Path],
        *,
        exec_deps: Callable[[], Any] | None = None,
        bus: Any = None,
        serves_channel: Callable[[str], bool] | None = None,
    ) -> None:
        self._workspace_resolver = workspace_resolver
        self._exec_deps = exec_deps
        self._bus = bus
        self._serves_channel = serves_channel
        # Strong refs to decisions still running (else GC'd mid-run).
        self._tasks: set[asyncio.Task] = set()

    @route(
        "POST",
        "/api/v1/approvals/{id}/decision",
        scope=Scope.ADMIN.value,
        request_model=ApprovalDecisionCommand,
        response_model=ApprovalDecisionResult,
        summary=(
            "Approve or reject a pending approval request as a person: a dashboard "
            "session only (API tokens are refused), with skills:write for a skill "
            "change, mcp:write for an MCP change, admin for anything else. 200 when "
            "the request was acted on; 409 when it was refused and left as it was."
        ),
    )
    async def decide(
        self, cmd: ApprovalDecisionCommand, principal: Principal,
    ) -> ApprovalDecisionResult:
        if principal.kind != "webui":
            raise ForbiddenError(
                "deciding an approval takes a person's dashboard session; a token "
                "issued for the API cannot approve",
                details={"principal": principal.kind},
            )
        from durin.agent import approval_store
        from durin.agent.approval_executors import ExecDeps

        workspace = self._workspace_resolver()
        record = approval_store.get(workspace, cmd.id)
        if record is None:
            raise NotFoundError(f"no approval request {cmd.id}", details={"approval_id": cmd.id})
        principal.require(decision_scope(record.get("kind")))

        deps = self._exec_deps() if self._exec_deps is not None else ExecDeps()
        if record.get("kind") == "exec_command":
            # A shell command runs only inside the turn that asked for it (the
            # literal command lives there alone). Strip the gateway's runner so
            # a decision from here could never run a different live command
            # under this record's approval; `decide` refuses it either way.
            deps = dataclasses.replace(deps, exec_run=None)

        task = asyncio.create_task(
            self._decide(workspace, cmd.id, cmd.decision, deps, decider_of(principal)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            outcome = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the caller still gets an answer
            raise DomainError(f"could not decide {cmd.id}: {exc}",
                              details={"approval_id": cmd.id}) from exc

        result = ApprovalDecisionResult(
            status=outcome.status, message=outcome.message, approval_id=cmd.id)
        if outcome.status == "refused":
            raise ConflictError(outcome.message, details=result.model_dump())
        return result

    async def _decide(self, workspace: Path, approval_id: str, decision: str, deps: Any,
                      decided_by: dict):
        """Decide, then tell the chat that asked (when one is owed). Runs as
        its own task, so both finish even when the request that started them
        is gone."""
        from durin.agent import approval
        from durin.agent.approval_notify import notify_origin

        outcome = await approval.decide(
            workspace, approval_id, decision, decided_by=decided_by, deps=deps)
        if self._serves_channel is not None:
            await notify_origin(self._bus, outcome, serves=self._serves_channel)
        return outcome
