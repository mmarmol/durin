"""AutomationsService — list, load, save, fire, and answer automation definitions.

Automations live as JSON at ``<workspace>/automations/<name>.json`` (see
``durin.automations.store``) and are validated by
``durin.automations.spec.parse_automation``. This is the HTTP surface the
webui automations view uses to manage automations and drive manual fires /
operator answers. A save or delete keeps the automation's schedule-trigger
cron jobs in sync via ``durin.automations.cron_sync``.

The runtime that actually executes an automation (``durin.automations.runtime.
AutomationsRuntime``) is wired in by the gateway (``durin.cli.commands``) and
passed here as ``runtime``; a surface with no runtime (e.g. spec-reading
tooling) leaves it ``None`` and ``fire``/``answer`` raise ``UnavailableError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

from durin.automations import queue, run_log
from durin.automations.cron_sync import remove_automation_jobs, sync_automation_jobs
from durin.automations.runtime import AutomationBusy
from durin.automations.spec import (
    AutomationError,
    AutomationNotFound,
    AutomationSpec,
    automation_to_dict,
    parse_automation,
)
from durin.automations.store import (
    delete_automation,
    list_automations,
    load_automation,
    save_automation,
)
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


class AutomationsListQuery(Query):
    """No inputs — lists every automation, each with its live counts and life state."""


class AutomationsListResult(Result):
    automations: list[dict[str, Any]]   # automation_to_dict() fields + active_runs/paused/pending_events/attempts/achieved/stuck


class AutomationGetQuery(Query):
    name: str


class AutomationGetResult(Result):
    name: str
    definition: dict[str, Any]   # automation_to_dict() shape


class AutomationSaveCommand(Command):
    name: str
    definition: dict[str, Any]


class AutomationSaveResult(Result):
    name: str


class AutomationDeleteCommand(Command):
    name: str


class AutomationDeleteResult(Result):
    deleted: bool


class AutomationFireCommand(Command):
    name: str
    task: str = ""


class AutomationFireResult(Result):
    run: dict[str, Any]   # the run manifest record (durin.automations.run_log shape)


class AutomationAnswerCommand(Command):
    name: str
    run_id: str
    text: str
    # An explicit action (webui buttons) bypasses durin.workflow.approval.parse_approval_reply's
    # keyword parsing — see AutomationsRuntime._answer.
    action: Literal["approve", "revise", "reject"] | None = None


class AutomationAnswerResult(Result):
    run: dict[str, Any]


class AutomationRunsQuery(Query):
    name: str
    limit: int = 50


class AutomationRunsResult(Result):
    runs: list[dict[str, Any]]   # newest-first run records for this automation


class AutomationsRunsQuery(Query):
    limit: int = 50


class AutomationsRunsResult(Result):
    runs: list[dict[str, Any]]   # newest-first run records across every automation


class AutomationsHooksSecretQuery(Query):
    """No inputs — returns the shared webhook ingress secret."""


class AutomationsHooksSecretResult(Result):
    secret: str
    path_template: str   # "/api/v1/hooks/{hook}" — the caller substitutes {hook}


def _counts(workspace: Path, name: str) -> dict[str, int]:
    active = run_log.active_runs(workspace, name)
    return {
        "active_runs": sum(1 for r in active if r.get("status") == "running"),
        "paused": sum(1 for r in active if r.get("status") == "paused"),
        "pending_events": queue.pending(workspace, name),
    }


def _life_state(workspace: Path, spec: AutomationSpec) -> dict[str, Any]:
    """``attempts`` — the current consecutive-unachieved streak (see
    ``run_log.consecutive_unachieved``). ``achieved`` — true only when the
    automation is disabled AND its most recent non-active run actually reached
    "achieved" (as opposed to being paused for any other reason, e.g. stuck).
    ``stuck`` — true once ``attempts`` reaches a configured ``life.max_attempts``,
    regardless of ``on_stuck`` mode, so the webui's LifeChip can show the streak
    is at its ceiling even when ``on_stuck`` is "notify"/"keep" and the
    automation is consequently still enabled."""
    attempts = run_log.consecutive_unachieved(workspace, spec.name)
    latest_terminal = next(
        (r for r in run_log.list_runs(workspace, spec.name, limit=None)
         if r.get("status") not in run_log.ACTIVE_STATUSES),
        None,
    )
    achieved = bool(
        latest_terminal is not None
        and latest_terminal.get("status") == "achieved"
        and not spec.enabled
    )
    stuck = bool(
        spec.life is not None
        and spec.life.max_attempts is not None
        and attempts >= spec.life.max_attempts
    )
    return {"attempts": attempts, "achieved": achieved, "stuck": stuck}


class AutomationsService:
    def __init__(
        self, workspace: Path, cron_service: Any = None, runtime: Any = None,
        hooks_secret: Callable[[], str] | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._cron_service = cron_service   # durin.cron.service.CronService — keeps trigger jobs in sync
        self._runtime = runtime             # durin.automations.runtime.AutomationsRuntime — None until cutover
        self._hooks_secret = hooks_secret   # () -> str, e.g. ApiTokenStore().get_or_create_hooks_secret

    @route(
        "GET", "/api/v1/automations",
        scope=Scope.AUTOMATIONS_READ.value,
        request_model=AutomationsListQuery, response_model=AutomationsListResult,
        summary="List all automation definitions, with live counts and life state.",
    )
    async def list(self, query: AutomationsListQuery, principal: Principal) -> AutomationsListResult:
        principal.require(Scope.AUTOMATIONS_READ)
        automations = []
        for spec in list_automations(self._workspace):
            automations.append({
                **automation_to_dict(spec),
                **_counts(self._workspace, spec.name),
                **_life_state(self._workspace, spec),
            })
        return AutomationsListResult(automations=automations)

    @route(
        "GET", "/api/v1/automations/{name}",
        scope=Scope.AUTOMATIONS_READ.value,
        request_model=AutomationGetQuery, response_model=AutomationGetResult,
        summary="Load one automation's full definition.",
    )
    async def get(self, query: AutomationGetQuery, principal: Principal) -> AutomationGetResult:
        principal.require(Scope.AUTOMATIONS_READ)
        try:
            spec = load_automation(self._workspace, query.name)
        except AutomationNotFound:
            raise NotFoundError(f"automation {query.name!r} not found")
        return AutomationGetResult(name=spec.name, definition=automation_to_dict(spec))

    @route(
        "PUT", "/api/v1/automations/{name}",
        scope=Scope.AUTOMATIONS_WRITE.value,
        request_model=AutomationSaveCommand, response_model=AutomationSaveResult,
        summary="Create or update an automation definition.",
    )
    async def save(self, cmd: AutomationSaveCommand, principal: Principal) -> AutomationSaveResult:
        principal.require(Scope.AUTOMATIONS_WRITE)
        # The URL is authoritative for identity, same precedent as
        # WorkflowsService.duplicate() overwriting the inner "name" field.
        definition = {**cmd.definition, "name": cmd.name}
        try:
            spec = parse_automation(definition)
            # save_automation itself re-validates the chain-trigger graph
            # (durin.automations.chains.validate_chain_edges) and raises the
            # same AutomationError on a cycle, so one except below covers both.
            save_automation(self._workspace, spec, actor="user", reason="saved in the automations editor")
        except AutomationError as exc:
            raise ValidationFailedError(f"invalid automation: {exc}")
        sync_automation_jobs(self._cron_service, spec)
        return AutomationSaveResult(name=spec.name)

    @route(
        "DELETE", "/api/v1/automations/{name}",
        scope=Scope.AUTOMATIONS_WRITE.value,
        request_model=AutomationDeleteCommand, response_model=AutomationDeleteResult,
        summary="Delete an automation definition.",
    )
    async def delete(self, cmd: AutomationDeleteCommand, principal: Principal) -> AutomationDeleteResult:
        principal.require(Scope.AUTOMATIONS_WRITE)
        try:
            delete_automation(self._workspace, cmd.name, actor="user",
                               reason="deleted in the automations editor")
        except AutomationNotFound:
            raise NotFoundError(f"automation {cmd.name!r} not found")
        remove_automation_jobs(self._cron_service, cmd.name)
        return AutomationDeleteResult(deleted=True)

    @route(
        "POST", "/api/v1/automations/{name}/fire",
        scope=Scope.AUTOMATIONS_WRITE.value,
        request_model=AutomationFireCommand, response_model=AutomationFireResult,
        summary="Manually fire an automation.",
    )
    async def fire(self, cmd: AutomationFireCommand, principal: Principal) -> AutomationFireResult:
        """Manually fire an automation ("Run now" in the webui detail view).

        ``cmd.task`` overrides the workflow's prompt for this one run. The
        webui's "Run now" button never prompts for one, so when it (or any
        other caller) leaves it blank, fall back to the FIRST schedule
        trigger's own ``task`` text, if the automation declares one — a
        scheduled automation fired manually then still gets the same prompt
        its clock trigger would have sent, instead of running with none at
        all. An automation with no schedule trigger (channel/webhook/chain
        only, or no triggers at all) instead falls back to a synthesized
        "Run the <workflow> workflow" — the same text ``durin.automations.
        migrate`` synthesizes for a cron trigger with no task text of its
        own — so ``task`` is never ``None`` from this route: the node runner
        has no placeholder for a missing task and renders one as the literal
        string "None" in the draft node's user message.
        """
        principal.require(Scope.AUTOMATIONS_WRITE)
        if self._runtime is None:
            raise UnavailableError("firing an automation is not available on this surface")
        task = cmd.task or None
        spec = None
        if task is None:
            try:
                spec = load_automation(self._workspace, cmd.name)
            except AutomationNotFound:
                spec = None
            if spec is not None:
                task = next((t.task or None for t in spec.triggers if t.source == "schedule"), None)
                if task is None:
                    task = f"Run the {spec.workflow} workflow"
        try:
            record = await self._runtime.fire(cmd.name, source="manual", task=task)
        except AutomationBusy as exc:
            raise ValidationFailedError(f"automation busy: {exc}")
        except AutomationNotFound as exc:
            raise NotFoundError(str(exc))
        return AutomationFireResult(run=record)

    @route(
        "POST", "/api/v1/automations/{name}/runs/{run_id}/answer",
        scope=Scope.AUTOMATIONS_WRITE.value,
        request_model=AutomationAnswerCommand, response_model=AutomationAnswerResult,
        summary="Answer an automation run awaiting an operator or a counterpart reply.",
    )
    async def answer(self, cmd: AutomationAnswerCommand, principal: Principal) -> AutomationAnswerResult:
        principal.require(Scope.AUTOMATIONS_WRITE)
        if self._runtime is None:
            raise UnavailableError("answering an automation run is not available on this surface")
        try:
            record = await self._runtime.answer(
                cmd.name, cmd.run_id, cmd.text, action=cmd.action, by="operator")
        except AutomationNotFound as exc:
            raise NotFoundError(str(exc))
        except ValueError as exc:
            raise ValidationFailedError(str(exc))
        return AutomationAnswerResult(run=record)

    @route(
        "GET", "/api/v1/automations/runs",
        scope=Scope.AUTOMATIONS_READ.value,
        request_model=AutomationsRunsQuery, response_model=AutomationsRunsResult,
        summary="Global activity feed across every automation, newest-first.",
    )
    async def runs_feed(self, query: AutomationsRunsQuery, principal: Principal) -> AutomationsRunsResult:
        principal.require(Scope.AUTOMATIONS_READ)
        return AutomationsRunsResult(runs=run_log.list_all_runs(self._workspace, query.limit))

    @route(
        "GET", "/api/v1/automations/{name}/runs",
        scope=Scope.AUTOMATIONS_READ.value,
        request_model=AutomationRunsQuery, response_model=AutomationRunsResult,
        summary="List one automation's persisted runs, newest-first.",
    )
    async def runs_list(self, query: AutomationRunsQuery, principal: Principal) -> AutomationRunsResult:
        principal.require(Scope.AUTOMATIONS_READ)
        return AutomationRunsResult(runs=run_log.list_runs(self._workspace, query.name, query.limit))

    @route(
        "GET", "/api/v1/automations/hooks-secret",
        scope=Scope.AUTOMATIONS_WRITE.value,
        request_model=AutomationsHooksSecretQuery, response_model=AutomationsHooksSecretResult,
        summary="Return the shared webhook ingress secret and its path template.",
    )
    async def hooks_secret(
        self, query: AutomationsHooksSecretQuery, principal: Principal,
    ) -> AutomationsHooksSecretResult:
        principal.require(Scope.AUTOMATIONS_WRITE)
        if self._hooks_secret is None:
            raise UnavailableError("the webhook ingress secret is not available on this surface")
        return AutomationsHooksSecretResult(secret=self._hooks_secret(), path_template="/api/v1/hooks/{hook}")
