"""POST /api/v1/approvals/{id}/decision — a person decides a pending request.

The service hands the decision to ``approval.decide`` with the gateway's live
handles and never re-implements its rules: an exec request is refused outside
its turn, a kind whose runner is missing is refused before the record moves, a
request past its TTL is expired instead of decided, and a verdict for a turn
still waiting is handed to that turn. What the service adds is who may decide
(a person's dashboard session, with the scope of the change's domain) and how
each outcome maps to HTTP: 200 whenever the request was acted on, 409 when it
was refused and left as it was.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone

import pytest

from durin.agent import approval_executors as ex
from durin.agent import approval_store as st
from durin.agent import pending_answers as pa
from durin.agent.approval import request as approval_request
from durin.bus.events import InboundMessage
from durin.service.approvals import (
    ApprovalDecisionCommand,
    ApprovalsService,
)
from durin.service.principal import Principal, Scope
from durin.service.types import ConflictError, ForbiddenError, NotFoundError

RUNS: list[dict] = []
STATE = {"hash": "h1"}


async def _run(ws, payload, deps):
    RUNS.append(payload)
    return {"ran": payload.get("name")}


@pytest.fixture(autouse=True)
def _fake_kinds(monkeypatch):
    # Load the real kinds first, so a later lazy load cannot re-register them
    # over the fakes below. Fake executors for the kinds these tests approve;
    # the real exec and skill_deps kinds stay, since their refusals are under
    # test.
    for module in ex._KIND_MODULES:
        importlib.import_module(module)
    for kind in ("skill_edit", "mcp_change"):
        monkeypatch.setitem(ex._REGISTRY, kind, (lambda ws, payload: STATE["hash"], _run))
    monkeypatch.delitem(ex._REQUIRES, "mcp_change", raising=False)
    RUNS.clear()
    STATE["hash"] = "h1"
    pa.reset()
    yield
    pa.reset()


class _Bus:
    def __init__(self) -> None:
        self.inbound: list[InboundMessage] = []

    async def publish_inbound(self, msg: InboundMessage) -> None:
        self.inbound.append(msg)


def _service(ws, *, bus=None, deps=None, serves=lambda channel: True) -> ApprovalsService:
    return ApprovalsService(
        workspace_resolver=lambda: ws,
        exec_deps=(lambda: deps) if deps is not None else None,
        bus=bus,
        serves_channel=serves,
    )


def _record(ws, kind="skill_edit", session="cron:nightly", **payload):
    return st.create(ws, kind=kind, summary=f"{kind} 'a'", detail={"diff": "d"},
                     payload={"name": "a", **payload}, change_hash="h1",
                     session_key=session, context="autonomous")


PERSON = Principal.webui("tok-webui", {Scope.ADMIN.value})


def _cmd(record_id: str, decision: str = "approve") -> ApprovalDecisionCommand:
    return ApprovalDecisionCommand(id=record_id, decision=decision)


# -- approve and reject ------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_runs_the_request_and_records_the_webui_decider(tmp_path):
    rec = _record(tmp_path)

    out = await _service(tmp_path).decide(_cmd(rec["id"]), PERSON)

    assert (out.status, out.approval_id) == ("applied", rec["id"])
    assert RUNS == [{"name": "a"}]
    stored = st.get(tmp_path, rec["id"])
    assert stored["status"] == "applied"
    assert stored["decided_by"] == {"kind": "user", "channel": "webui"}


@pytest.mark.asyncio
async def test_reject_closes_the_request_without_running_it(tmp_path):
    rec = _record(tmp_path)

    out = await _service(tmp_path).decide(_cmd(rec["id"], "reject"), PERSON)

    assert out.status == "rejected" and RUNS == []
    assert st.get(tmp_path, rec["id"])["status"] == "rejected"


# -- refusals decide already makes -------------------------------------------


@pytest.mark.asyncio
async def test_an_exec_request_is_refused_outside_its_turn_and_stays_pending(tmp_path):
    rec = _record(tmp_path, kind="exec_command", command="rm -rf build", cwd=str(tmp_path))
    # The gateway's live handles carry a runner, yet only the turn that asked
    # holds the literal command: approving it here must be refused.
    deps = ex.ExecDeps(exec_run=lambda **_: None)

    with pytest.raises(ConflictError) as refused:
        await _service(tmp_path, deps=deps).decide(_cmd(rec["id"]), PERSON)

    assert refused.value.details["status"] == "refused"
    assert refused.value.details["approval_id"] == rec["id"]
    assert "only be approved in the chat that asked" in refused.value.message
    assert st.get(tmp_path, rec["id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_a_missing_runner_is_refused_and_the_request_stays_pending(tmp_path):
    rec = _record(tmp_path, kind="skill_deps", specs=[])

    with pytest.raises(ConflictError) as refused:
        await _service(tmp_path, deps=ex.ExecDeps()).decide(_cmd(rec["id"]), PERSON)

    assert "shell runner" in refused.value.message
    assert st.get(tmp_path, rec["id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_a_request_past_its_ttl_is_reported_stale_and_expired(tmp_path):
    rec = _record(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    st.transition(tmp_path, rec["id"], expect=("pending",), to="pending", expires_at=past)

    out = await _service(tmp_path).decide(_cmd(rec["id"]), PERSON)

    assert out.status == "stale" and "expired" in out.message
    assert st.get(tmp_path, rec["id"])["status"] == "expired" and RUNS == []


@pytest.mark.asyncio
async def test_a_request_whose_target_changed_is_reported_stale(tmp_path):
    rec = _record(tmp_path)
    STATE["hash"] = "changed"

    out = await _service(tmp_path).decide(_cmd(rec["id"]), PERSON)

    assert out.status == "stale" and RUNS == []
    assert st.get(tmp_path, rec["id"])["status"] == "stale"


@pytest.mark.asyncio
async def test_a_request_decided_already_is_refused(tmp_path):
    rec = _record(tmp_path)
    service = _service(tmp_path)
    await service.decide(_cmd(rec["id"], "reject"), PERSON)

    with pytest.raises(ConflictError) as refused:
        await service.decide(_cmd(rec["id"]), PERSON)

    assert "already decided" in refused.value.message


@pytest.mark.asyncio
async def test_an_unknown_request_is_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await _service(tmp_path).decide(_cmd("0123456789ab"), PERSON)


# -- who may decide ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [
    Principal.remote("api-token", {Scope.ADMIN.value}),
    Principal.remote("static", {Scope.ADMIN.value}),
    Principal.local(),
])
async def test_only_a_dashboard_session_may_decide(tmp_path, principal):
    rec = _record(tmp_path)

    with pytest.raises(ForbiddenError):
        await _service(tmp_path).decide(_cmd(rec["id"]), principal)

    assert st.get(tmp_path, rec["id"])["status"] == "pending" and RUNS == []


@pytest.mark.asyncio
async def test_the_scope_follows_the_kind_of_change(tmp_path):
    skills_only = Principal.webui("tok", {Scope.SKILLS_WRITE.value})
    mcp_only = Principal.webui("tok", {Scope.MCP_WRITE.value})
    edit = _record(tmp_path, kind="skill_edit")
    mcp = _record(tmp_path, kind="mcp_change")
    exec_rec = _record(tmp_path, kind="exec_command", command="ls", cwd=str(tmp_path))
    service = _service(tmp_path)

    with pytest.raises(ForbiddenError):
        await service.decide(_cmd(edit["id"]), mcp_only)
    with pytest.raises(ForbiddenError):
        await service.decide(_cmd(mcp["id"]), skills_only)
    # A shell command belongs to no domain scope: only admin covers it.
    with pytest.raises(ForbiddenError):
        await service.decide(_cmd(exec_rec["id"]), skills_only)

    assert (await service.decide(_cmd(edit["id"]), skills_only)).status == "applied"
    assert (await service.decide(_cmd(mcp["id"]), mcp_only)).status == "applied"


def test_the_command_takes_only_a_well_formed_id_and_a_known_decision():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ApprovalDecisionCommand(id="../../etc/passwd", decision="approve")
    with pytest.raises(ValidationError):
        ApprovalDecisionCommand(id="0123456789ab", decision="maybe")


# -- the chat that asked hears the outcome -------------------------------------


@pytest.mark.asyncio
async def test_a_decision_outside_the_asking_turn_notifies_that_chat(tmp_path):
    bus = _Bus()
    rec = _record(tmp_path, session="websocket:chat-1")

    await _service(tmp_path, bus=bus).decide(_cmd(rec["id"]), PERSON)

    assert len(bus.inbound) == 1
    note = bus.inbound[0]
    assert note.channel == "system" and note.session_key_override == "websocket:chat-1"
    assert note.content.count("Approved: skill_edit 'a' — result: ") == 1


@pytest.mark.asyncio
async def test_a_rejection_outside_the_asking_turn_notifies_that_chat(tmp_path):
    bus = _Bus()
    rec = _record(tmp_path, session="slack:C1:1712.0001")

    await _service(tmp_path, bus=bus).decide(_cmd(rec["id"], "reject"), PERSON)

    assert [m.chat_id for m in bus.inbound] == ["slack:C1"]
    assert "Rejected: skill_edit 'a'" in bus.inbound[0].content


@pytest.mark.asyncio
async def test_a_request_from_an_autonomous_run_notifies_nobody(tmp_path):
    bus = _Bus()
    rec = _record(tmp_path, session="cron:nightly")

    await _service(tmp_path, bus=bus).decide(_cmd(rec["id"]), PERSON)

    assert bus.inbound == []


@pytest.mark.asyncio
async def test_a_verdict_handed_to_the_waiting_turn_posts_no_note(tmp_path):
    bus = _Bus()
    session_key = "websocket:chat-2"
    prepared = ex.Prepared(kind="skill_edit", summary="skill_edit 'a'", detail={},
                           payload={"name": "a"}, change_hash="h1")

    async def ask(record):
        return await pa.create(session_key, kind="approval", ref=record["id"])

    turn = asyncio.ensure_future(approval_request(
        tmp_path, prepared, session_key=session_key, deps=ex.ExecDeps(), ask=ask))
    await asyncio.sleep(0)
    waiting_id = pa.waiting_ref(session_key)
    assert waiting_id is not None

    out = await _service(tmp_path, bus=bus).decide(_cmd(waiting_id), PERSON)
    result = await turn

    assert out.status == "pending"
    assert result.status == "applied"
    assert result.record["decided_by"] == {"kind": "user", "channel": "webui"}
    assert bus.inbound == []


# -- the decision outlives its request -----------------------------------------


@pytest.mark.asyncio
async def test_a_cancelled_request_does_not_cancel_the_decision(tmp_path, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(ws, payload, deps):
        started.set()
        await release.wait()
        return {"ran": True}

    monkeypatch.setitem(ex._REGISTRY, "skill_edit", (lambda ws, payload: "h1", _slow))
    rec = _record(tmp_path)
    service = _service(tmp_path)

    call = asyncio.ensure_future(service.decide(_cmd(rec["id"]), PERSON))
    await started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    release.set()
    await asyncio.gather(*list(service._tasks))

    assert st.get(tmp_path, rec["id"])["status"] == "applied"
