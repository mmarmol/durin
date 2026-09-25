"""Authority by context for privileged agent actions.

An action that introduces or modifies EXECUTABLE state — an MCP server, a
skill, a dependency install, a command the exec policy refused — must not be
authorized by a value the model wrote. A field in a tool call is a claim by
the model, never evidence about the world outside it.

Authority is a property of the execution CONTEXT, which the runtime owns: the
session key is minted by the gateway (``websocket:``, ``cron:``,
``workflow:``…), never by the model. The gated tools settle what they can
themselves (the exec hard floor, the operator's ``install_policy`` and
``allow_patterns``, a request with nothing to decide) and hand the rest to
``request``, which settles it in one of three ways: the skills judge clears
it, a person in this chat is asked while the turn waits, or it is filed as a
durable pending record (``approval_store``) that a person decides later. A
turn with input from an API token is never asked in the chat. ``decide``
resolves a record from outside the turn that filed it.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from durin.agent import approval_store
from durin.agent.approval_executors import (
    ExecDeps,
    Prepared,
    current_hash,
    execute,
)

__all__ = [
    "AUTONOMOUS_SESSION_PREFIXES",
    "INTERACTIVE_SESSION_PREFIXES",
    "Outcome",
    "decide",
    "human_reachable",
    "is_interactive",
    "note_turn_input",
    "outcome_to_tool_result",
    "request",
    "turn_has_api_input",
]

# Session-key prefixes the runtime uses for contexts with no person attached.
# Matched as a plain prefix, so entries must include every separator variant
# actually minted (``cron_dream`` has no colon; ``cron:`` does).
AUTONOMOUS_SESSION_PREFIXES: tuple[str, ...] = (
    "cron:", "cron_dream", "system:", "workflow:", "reactive_dream",
    "dream_supervisor", "gateway", "loop:", "subagent:", "automation:",
)

# Chat-bearing prefixes: a person is on the other end of these. Anything not
# listed is treated as autonomous — an unrecognised context is not a person.
INTERACTIVE_SESSION_PREFIXES: tuple[str, ...] = (
    "websocket:", "cli:", "slack:", "discord:", "telegram:", "whatsapp:",
    "matrix:", "email:", "msteams:", "unified:", "feishu:", "dingtalk:",
    "wecom:", "weixin:", "qq:",
)


def human_reachable(session_key: str | None) -> bool:
    """True when a person could actually see and answer in this context.

    Requires BOTH a chat-bearing session kind and a live inbound consumer to
    deliver the answer (``pending_answers.can_block``'s process-level flag).
    Unknown session kinds are autonomous: fail closed.
    """
    if not session_key or not session_key.startswith(INTERACTIVE_SESSION_PREFIXES):
        return False
    from durin.agent import pending_answers

    return pending_answers.consumer_active()


# Set for the rest of a turn once any of its input came from an API token
# rather than a person at a chat surface. A token holder is a program: a
# ``chat:write`` token may converse in a webui conversation, but it must not
# carry the person's authority to approve privileged actions there. Turns run
# in their own task, so the flag never outlives the turn that set it.
_TURN_HAS_API_INPUT: ContextVar[bool] = ContextVar("approval_turn_has_api_input", default=False)


def note_turn_input(metadata: dict[str, Any] | None) -> None:
    """Record that the current turn received input from *metadata*'s sender.

    Called for the message that opens a turn and for every message injected
    into it. Input marked ``origin: "api"`` makes ``turn_has_api_input`` true
    for the rest of the turn."""
    if metadata and metadata.get("origin") == "api":
        _TURN_HAS_API_INPUT.set(True)


def turn_has_api_input() -> bool:
    """True once the current turn received input from an API token.

    Such a turn never puts a privileged request to the person in the chat:
    the in-chat asker is withheld, so skill and MCP changes are filed as
    pending and an exec command that needs approval is refused, as in a
    context with no person. The operator's standing policy (a judge,
    ``install_policy: auto``) still applies; it is not the turn's authority."""
    return _TURN_HAS_API_INPUT.get()


JudgeFn = Callable[[], Awaitable[str | None]]
AskFn = Callable[[dict], Awaitable[str | None]]


@dataclass(frozen=True)
class Outcome:
    status: str  # applied | rejected | pending | failed | stale | refused
    record: dict | None = None
    result: dict | None = None
    message: str = ""


def is_interactive(session_key: str | None) -> bool:
    """A chat-bearing session kind (a person is on the other end)."""
    return bool(session_key) and session_key.startswith(INTERACTIVE_SESSION_PREFIXES)


async def _run_approved(workspace: Path | str, record: dict, deps: ExecDeps) -> Outcome:
    """Execute a record already in ``approved``: re-check its hash, run it once."""
    try:
        now_hash = current_hash(Path(workspace), record)
    except Exception as exc:  # noqa: BLE001 — any hash failure ends the record, never hangs it
        rec = approval_store.transition(workspace, record["id"], expect=("approved",),
                                        to="failed", result={"error": str(exc)})
        return Outcome("failed", rec, None, f"Could not run {record['summary']}: {exc}")
    if now_hash != record.get("change_hash"):
        rec = approval_store.transition(workspace, record["id"], expect=("approved",), to="stale")
        return Outcome("stale", rec, None,
                       f"Not run: {record['summary']} — the target changed after it was "
                       "requested, so the approval no longer matches it. Ask again if still needed.")
    # Executors record who approved (commit trailers, audit log) from here.
    deps.extra["approval"] = {"id": record["id"], "decided_by": record.get("decided_by")}
    try:
        result = await execute(Path(workspace), record, deps)
    except asyncio.CancelledError:
        # A cancelled run must not leave the record stuck in "approved" —
        # nothing else ever retries or expires that state.
        approval_store.transition(workspace, record["id"], expect=("approved",),
                                  to="failed", result={"error": "cancelled"})
        raise
    except Exception as exc:  # noqa: BLE001 — a failed run is recorded, never raised to the turn
        rec = approval_store.transition(workspace, record["id"], expect=("approved",),
                                        to="failed", result={"error": str(exc)})
        return Outcome("failed", rec, None, f"Approved, but running it failed: {exc}")
    rec = approval_store.transition(workspace, record["id"], expect=("approved",),
                                    to="applied", result=result)
    return Outcome("applied", rec, result, f"Done: {record['summary']}.")


def _pending_outcome(record: dict, *, asked: bool, api_input: bool = False) -> Outcome:
    if asked:
        where = "the user did not answer; it is still waiting for approval (`durin approvals`)"
    elif api_input:
        # Say why nobody was asked, so the model can tell the person where
        # the request waits instead of retrying it in the chat.
        where = ("this turn includes input from an API token, which cannot approve it, "
                 "so it is waiting for a person's approval (`durin approvals`)")
    else:
        where = "waiting for approval (`durin approvals`)"
    return Outcome("pending", record, None, (
        f"Not done yet: {record['summary']} — {where}, id {record['id']}. Continue "
        "without it; do not retry, and do not reach the same effect another way."))


async def request(workspace: Path | str, prepared: Prepared, *, session_key: str | None,
                  deps: ExecDeps, judge: JudgeFn | None = None,
                  ask: AskFn | None = None) -> Outcome:
    """Decide and, when allowed, run one privileged request.

    Order: the judge (when the caller made the request judge-eligible) may clear
    it; otherwise a person in this chat is asked (``ask``); otherwise it is
    filed as pending. Nothing here reads a value the model wrote.
    """
    context = "interactive" if is_interactive(session_key) else "autonomous"
    if prepared.kind in ("skill_deps", "mcp_change", "exec_command"):
        # The judge only ever clears skill installs/edits; deps, MCP changes
        # and exec commands always need a person or end up pending.
        judge = None
    if judge is not None:
        verdict = None
        try:
            verdict = await judge()
        except Exception:  # noqa: BLE001 — an unavailable judge falls through to a person
            verdict = None
        if verdict == "safe":
            existing = approval_store.find_pending(
                workspace, session_key=session_key, kind=prepared.kind,
                change_hash=prepared.change_hash)
            if existing is not None:
                record = approval_store.transition(
                    workspace, existing["id"], expect=("pending",), to="approved",
                    decided_by={"kind": "judge"})
                if record is None:
                    return _already_decided(workspace, existing["id"])
            else:
                record = approval_store.create(
                    workspace, kind=prepared.kind, summary=prepared.summary,
                    detail=prepared.detail, payload=prepared.payload,
                    change_hash=prepared.change_hash, session_key=session_key,
                    context=context, status="approved", decided_by={"kind": "judge"})
            return await _run_approved(workspace, record, deps)

    record = approval_store.find_pending(
        workspace, session_key=session_key, kind=prepared.kind,
        change_hash=prepared.change_hash) or approval_store.create(
        workspace, kind=prepared.kind, summary=prepared.summary, detail=prepared.detail,
        payload=prepared.payload, change_hash=prepared.change_hash,
        session_key=session_key, context=context)

    if ask is None:
        return _pending_outcome(record, asked=False, api_input=turn_has_api_input())
    answer = await ask(record)
    if answer not in ("approve", "reject"):
        return _pending_outcome(record, asked=True)
    return await _apply_decision(workspace, record["id"], answer,
                                 decided_by={"kind": "user", "channel": session_key},
                                 deps=deps)


async def _apply_decision(workspace: Path | str, approval_id: str, decision: str, *,
                          decided_by: dict, deps: ExecDeps) -> Outcome:
    if decision == "reject":
        rec = approval_store.transition(workspace, approval_id, expect=("pending",),
                                        to="rejected", decided_by=decided_by)
        if rec is None:
            return _already_decided(workspace, approval_id)
        return Outcome("rejected", rec, None, (
            f"The user declined: {rec['summary']}. Do not retry, and do not reach the "
            "same effect another way."))
    if decision != "approve":
        return Outcome("refused", None, None, f"Unknown decision {decision!r}.")
    rec = approval_store.transition(workspace, approval_id, expect=("pending",),
                                    to="approved", decided_by=decided_by)
    if rec is None:
        return _already_decided(workspace, approval_id)
    return await _run_approved(workspace, rec, deps)


def _already_decided(workspace: Path | str, approval_id: str) -> Outcome:
    rec = approval_store.get(workspace, approval_id)
    if rec is None:
        return Outcome("refused", None, None, f"No approval request {approval_id}.")
    return Outcome("refused", rec, None,
                   f"Approval {approval_id} was already decided ({rec.get('status')}).")


async def decide(workspace: Path | str, approval_id: str, decision: str, *,
                 decided_by: dict, deps: ExecDeps) -> Outcome:
    """Resolve a request from outside the turn that filed it (Pending, CLI, API,
    a webui click). A pending record past its TTL is expired here instead of
    decided, since a person could otherwise approve a stale request nobody
    re-checked. If that turn is still waiting on it, hand the verdict to the
    waiter so the turn runs it and continues. Otherwise decide and run here."""
    if decision not in ("approve", "reject"):
        return Outcome("refused", None, None, f"Unknown decision {decision!r}.")
    rec = approval_store.get(workspace, approval_id)
    if rec is None:
        return Outcome("refused", None, None, f"No approval request {approval_id}.")
    if rec.get("legacy"):
        return Outcome("refused", rec, None, (
            f"{approval_id} is a legacy request with no recorded payload; it can only "
            "be discarded. Ask the agent to request it again."))
    if rec.get("status") == "pending":
        expires_at = rec.get("expires_at")
        if expires_at and datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
            expired = approval_store.transition(workspace, approval_id, expect=("pending",),
                                                to="expired")
            if expired is not None:
                return Outcome("stale", expired, None, (
                    f"Not decided: {expired['summary']} — this request expired, and can "
                    "no longer be applied. Ask again if it is still wanted."))
            # Lost the race: someone else moved it out of "pending" first. Fall
            # through — the ordinary decision path below sees the real status.
    session_key = rec.get("requested_by_session")
    from durin.agent import pending_answers

    if session_key and pending_answers.waiting_ref(session_key) == approval_id:
        if pending_answers.resolve(session_key, decision):
            return Outcome("pending", rec, None, "Handed to the waiting turn.")
    return await _apply_decision(workspace, approval_id, decision,
                                 decided_by=decided_by, deps=deps)


def outcome_to_tool_result(outcome: Outcome) -> dict:
    """What a gated tool returns to the model."""
    out: dict = {"status": outcome.status, "message": outcome.message}
    if outcome.record is not None:
        out["approval_id"] = outcome.record.get("id")
    if outcome.result is not None:
        out["result"] = outcome.result
    return out
