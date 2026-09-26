"""PendingService — everything that waits on a person, in one list.

``GET /api/v1/pending`` merges six sources. Each is read through its own
listing function, the one behind its own route, which stays the source of
truth; this route only aggregates:

* ``approval`` — privileged requests the agent filed for a person
  (``approval_store``), expired and pruned first as ``durin approvals`` does.
  Legacy records are left out: they carry no payload, so the only thing to do
  with one is ``durin approvals discard``.
* ``skill_quarantine`` — skill imports awaiting a decision
  (``skills_surface.quarantined_skills``). An import a pending ``skill_install``
  request names is left out: that request is the item, and deciding it
  settles the import too.
* ``automation_run`` — automation runs paused on an approval or a question
  (``automations.run_log``).
* ``workflow_run`` — workflow runs waiting for input that can be resumed
  (``workflow.run_log``). A run an automation started is left out: it shows as
  that automation's paused run instead of twice.
* ``flagged_pair`` — memory pairs the dream flagged for review
  (``service.memory.flagged_pair_list``).
* ``skill_suggestion`` — curation suggestions for manual skills
  (``service.skills.suggestion_list``).

Each item is ``{source, id, kind, title, summary, created_at, resolve, data}``.
``data`` is the source's own record in the shape its own route returns, so a
client renders it with the card it already has for that source. ``resolve``
says how to act on it: a ``form`` naming the kind of resolution and the
``actions`` — each one the method and path of the route that does it (the
new decision route for approvals, the source's existing route for the rest),
plus ``body`` for the request fields the item fixes; the rest of each body is
that route's own contract.

A source is shown only to a principal holding the scope its own listing route
requires (for approvals, the read scope of each request's kind); ``admin``
covers them all. A source that fails to load is reported in ``errors`` while
the others are still listed, so one broken store never hides the rest.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from loguru import logger

from durin.service.approvals import read_scope
from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import ForbiddenError, Query, Result

# The order sources are listed in when items share a timestamp, and the order
# their sections read in.
SOURCE_ORDER: tuple[str, ...] = (
    "approval", "skill_quarantine", "automation_run", "workflow_run",
    "flagged_pair", "skill_suggestion",
)

# Every source but approvals, with the listing route of its own whose scope
# it shares. Approvals have no listing route: each request takes the read
# scope of its kind (``approvals.read_scope``).
SOURCE_ROUTES: dict[str, tuple[str, Scope]] = {
    "skill_quarantine": ("/api/v1/skills/quarantine", Scope.SKILLS_READ),
    "automation_run": ("/api/v1/automations/runs", Scope.AUTOMATIONS_READ),
    "workflow_run": ("/api/v1/workflows/runs", Scope.WORKFLOWS_READ),
    "flagged_pair": ("/api/v1/memory/flagged-pairs", Scope.MEMORY_READ),
    "skill_suggestion": ("/api/v1/skills/suggestions", Scope.SKILLS_READ),
}

# The scopes an approval request can be read with, one per kind family.
_APPROVAL_READ_SCOPES = (Scope.SKILLS_READ, Scope.MCP_READ, Scope.ADMIN)

_SUMMARY_MAX_CHARS = 200


class PendingQuery(Query):
    """No inputs — returns everything pending that the caller may read."""


class PendingAction(Result):
    name: str
    method: str
    path: str
    # The request-body fields this item fixes; the rest comes from the form.
    body: dict[str, Any] | None = None


class PendingResolve(Result):
    form: str
    actions: list[PendingAction]


class PendingItem(Result):
    source: str
    id: str
    kind: str
    title: str
    summary: str
    created_at: str | None
    resolve: PendingResolve
    data: dict[str, Any]


class PendingSourceError(Result):
    source: str
    detail: str


class PendingResult(Result):
    items: list[PendingItem]
    count: int
    errors: list[PendingSourceError] = []


def _seg(value: Any) -> str:
    """One URL path segment."""
    return quote(str(value), safe="")


def _cap(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= _SUMMARY_MAX_CHARS else text[:_SUMMARY_MAX_CHARS - 1] + "…"


def _iso_from_epoch(seconds: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _approval_items(workspace: Path, principal: Principal,
                    represented: set[str]) -> list[PendingItem]:
    """The pending approval requests *principal* may read. Adds to
    *represented* the quarantined imports that listed install requests name,
    so the quarantine source does not list them a second time."""
    from durin.agent import approval_store

    # A request past its TTL must never show as pending just because nobody
    # decided or listed it since it aged out.
    approval_store.expire_and_prune(workspace)
    items: list[PendingItem] = []
    for rec in approval_store.list_records(workspace, status="pending", include_legacy=False):
        kind = rec.get("kind") or ""
        if not principal.has_scope(read_scope(kind)):
            continue
        if kind == "skill_install":
            quarantine = (rec.get("payload") or {}).get("quarantine")
            if quarantine:
                represented.add(str(quarantine))
        path = f"/api/v1/approvals/{_seg(rec['id'])}/decision"
        session = rec.get("requested_by_session")
        items.append(PendingItem(
            source="approval",
            id=rec["id"],
            kind=kind,
            title=rec.get("summary") or kind,
            summary=f"Requested by {session}" if session else "Requested by an autonomous run",
            created_at=rec.get("requested_at") or None,
            resolve=PendingResolve(form="approval", actions=[
                PendingAction(name=decision, method="POST", path=path,
                              body={"decision": decision})
                for decision in ("approve", "reject")
            ]),
            data={
                "approval_id": rec["id"],
                "kind": kind,
                "summary": rec.get("summary") or "",
                "detail": rec.get("detail") or {},
                "requested_by_session": session,
                "context": rec.get("context"),
                "requested_at": rec.get("requested_at"),
                "expires_at": rec.get("expires_at"),
            },
        ))
    return items


def _skill_quarantine_items(workspace: Path, represented: set[str]) -> list[PendingItem]:
    """Quarantined imports awaiting a decision, but for those an install
    request listed among the approvals already represents."""
    from durin.agent.skills_surface import quarantined_skills

    items: list[PendingItem] = []
    for entry in quarantined_skills(workspace):
        name = entry["name"]
        if name in represented:
            continue
        verdict = entry.get("verdict") or "not scanned"
        source = entry.get("source") or "an unknown source"
        items.append(PendingItem(
            source="skill_quarantine",
            id=name,
            kind="skill_import",
            title=name,
            summary=f"Skill import awaiting a decision — scan verdict {verdict}, from {source}",
            created_at=entry.get("quarantined_at"),
            resolve=PendingResolve(form="skill_import", actions=[
                PendingAction(name="approve", method="POST",
                              path=f"/api/v1/skills/{_seg(name)}/approve"),
                PendingAction(name="reject", method="DELETE",
                              path=f"/api/v1/skills/{_seg(name)}/quarantine"),
            ]),
            data=entry,
        ))
    return items


def _automation_run_items(workspace: Path) -> list[PendingItem]:
    from durin.automations import run_log

    items: list[PendingItem] = []
    # Paused runs are exempt from the listing's cap, so a cap of one keeps
    # every one of them and skips reading back the finished history.
    for run in run_log.list_all_runs(workspace, limit=1):
        if run.get("status") != "paused":
            continue
        name, run_id = run.get("automation") or "", run.get("run_id") or ""
        base = f"/api/v1/automations/{_seg(name)}/runs/{_seg(run_id)}"
        items.append(PendingItem(
            source="automation_run",
            id=run_id,
            kind=run.get("ask_kind") or "question",
            title=name,
            summary=_cap(run.get("ask") or run.get("proposal")),
            # The last write to a paused run is the pause itself.
            created_at=_iso_from_epoch(run.get("ts") or run.get("started_at")),
            resolve=PendingResolve(form="automation_answer", actions=[
                PendingAction(name="answer", method="POST", path=f"{base}/answer"),
                PendingAction(name="stop", method="POST", path=f"{base}/stop"),
            ]),
            data=run,
        ))
    return items


def _workflow_run_items(workspace: Path) -> list[PendingItem]:
    from durin.workflow import run_log

    items: list[PendingItem] = []
    # needs_input runs are exempt from the listing's cap (see above).
    for run in run_log.list_all_runs(workspace, limit=1):
        if run.get("status") != "needs_input" or not run.get("needs_input_node"):
            continue
        if str(run.get("origin") or "").startswith("automation:"):
            # An automation's own run: its paused automation run is the item.
            continue
        name, run_id = run.get("workflow") or "", run.get("run_id") or ""
        items.append(PendingItem(
            source="workflow_run",
            id=run_id,
            kind=run.get("ask_kind") or "question",
            title=name,
            summary=_cap(run.get("questions") or run.get("task")),
            # A paused run is finalized when it pauses.
            created_at=_iso_from_epoch(run.get("finished_at") or run.get("started_at")),
            resolve=PendingResolve(form="workflow_resume", actions=[
                PendingAction(name="resume", method="POST",
                              path=f"/api/v1/workflows/{_seg(name)}/run",
                              body={"resume_run_id": run_id}),
            ]),
            data=run,
        ))
    return items


def _flagged_pair_items(workspace: Path) -> list[PendingItem]:
    from durin.service.memory import flagged_pair_list

    items: list[PendingItem] = []
    for pair in flagged_pair_list(workspace):
        items.append(PendingItem(
            source="flagged_pair",
            id=f"{pair.ref_a}|{pair.ref_b}",
            kind="memory_pair",
            title=f"{pair.ref_a} ↔ {pair.ref_b}",
            summary=_cap(pair.reasoning),
            created_at=_iso_from_epoch(pair.at_ms / 1000) if pair.at_ms is not None else None,
            resolve=PendingResolve(form="pair_resolution", actions=[
                PendingAction(name="resolve", method="POST",
                              path="/api/v1/memory/flagged-pairs/resolve",
                              body={"ref_a": pair.ref_a, "ref_b": pair.ref_b}),
            ]),
            data=pair.model_dump(),
        ))
    return items


def _skill_suggestion_items(workspace: Path) -> list[PendingItem]:
    from durin.service.skills import suggestion_list

    items: list[PendingItem] = []
    for suggestion in suggestion_list(workspace):
        base = f"/api/v1/skills/suggestions/{_seg(suggestion.id)}"
        items.append(PendingItem(
            source="skill_suggestion",
            id=suggestion.id,
            kind=suggestion.type or "suggestion",
            title=suggestion.skill,
            summary=_cap(suggestion.reason),
            created_at=suggestion.created_at or None,
            resolve=PendingResolve(form="suggestion_review", actions=[
                PendingAction(name="accept", method="POST", path=f"{base}/accept"),
                PendingAction(name="reject", method="POST", path=f"{base}/reject"),
            ]),
            data=suggestion.model_dump(),
        ))
    return items


def _newest_first_key(item: PendingItem) -> tuple[float, int]:
    try:
        stamp = datetime.fromisoformat(item.created_at or "")
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        seconds = stamp.timestamp()
    except ValueError:
        seconds = float("-inf")
    return (seconds, -SOURCE_ORDER.index(item.source))


def collect_pending(workspace: Path, principal: Principal) -> PendingResult:
    """Every item waiting on a person that *principal* may read, newest first."""
    # Quarantined imports that a listed install request names. Filled by the
    # approval source, which runs first (SOURCE_ORDER); if it fails to load,
    # nothing is represented and every import still lists.
    represented: set[str] = set()
    fetchers: dict[str, Callable[[], list[PendingItem]]] = {
        "approval": lambda: _approval_items(workspace, principal, represented),
        "skill_quarantine": lambda: _skill_quarantine_items(workspace, represented),
        "automation_run": lambda: _automation_run_items(workspace),
        "workflow_run": lambda: _workflow_run_items(workspace),
        "flagged_pair": lambda: _flagged_pair_items(workspace),
        "skill_suggestion": lambda: _skill_suggestion_items(workspace),
    }
    items: list[PendingItem] = []
    errors: list[PendingSourceError] = []
    readable = 0
    for source in SOURCE_ORDER:
        if source == "approval":
            allowed = any(principal.has_scope(s) for s in _APPROVAL_READ_SCOPES)
        else:
            allowed = principal.has_scope(SOURCE_ROUTES[source][1])
        if not allowed:
            continue
        readable += 1
        try:
            items.extend(fetchers[source]())
        except Exception as exc:  # noqa: BLE001 — one broken store must not hide the rest
            logger.exception("pending: could not list {}", source)
            errors.append(PendingSourceError(source=source, detail=str(exc)))
    if readable == 0:
        raise ForbiddenError(
            "the pending list needs the read scope of at least one of its sources",
            details={"scopes": sorted({Scope.SKILLS_READ.value, Scope.MCP_READ.value}
                                      | {scope.value for _, scope in SOURCE_ROUTES.values()})},
        )
    items.sort(key=_newest_first_key, reverse=True)
    return PendingResult(items=items, count=len(items), errors=errors)


class PendingService:
    """The Pending list. ``workspace_resolver`` returns the gateway workspace."""

    def __init__(self, workspace_resolver: Callable[[], Path]) -> None:
        self._workspace_resolver = workspace_resolver

    @route(
        "GET",
        "/api/v1/pending",
        scope=Scope.ADMIN.value,
        request_model=PendingQuery,
        response_model=PendingResult,
        summary=(
            "Everything waiting on a person: approval requests, skill imports in "
            "quarantine, paused automation runs, workflow runs waiting for input (not "
            "an automation's), memory pairs the dream flagged, and skill suggestions. "
            "Each source shows with the read scope of its own listing route; admin "
            "covers them all."
        ),
    )
    async def list(self, query: PendingQuery, principal: Principal) -> PendingResult:
        # Every source reads files; keep that off the event loop.
        return await asyncio.to_thread(collect_pending, self._workspace_resolver(), principal)
