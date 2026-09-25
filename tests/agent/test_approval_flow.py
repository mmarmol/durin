"""approval.request / decide: judge, ask, pending, stale, exactly-once."""
import asyncio

import pytest

from durin.agent import approval
from durin.agent import approval_executors as ex
from durin.agent import approval_store as st
from durin.agent import pending_answers as pa

RUNS: list[dict] = []
SEEN_APPROVAL: list[dict] = []
STATE = {"hash": "h1"}


async def _run(ws, payload, deps):
    # Executors receive the recorded payload, not the whole record, plus who
    # approved it in deps.extra["approval"].
    RUNS.append(payload)
    SEEN_APPROVAL.append(dict(deps.extra.get("approval") or {}))
    return {"ok": True}


PREP = ex.Prepared(kind="skill_edit", summary="edit skill 'a'", detail={"diff": "d"},
                   payload={"name": "a"}, change_hash="h1")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Swap in a fake executor for this module only; the real skill_edit
    # executor must stay registered for every other test module.
    monkeypatch.setitem(ex._REGISTRY, "skill_edit",
                        (lambda ws, payload: STATE["hash"], _run))
    RUNS.clear()
    SEEN_APPROVAL.clear()
    STATE["hash"] = "h1"
    pa.reset()
    yield
    pa.reset()


async def _judge_safe():
    return "safe"


async def _judge_caution():
    return "caution"


def _asker(answer):
    async def ask(record):
        return answer
    return ask


@pytest.mark.asyncio
async def test_judge_safe_applies_without_asking(tmp_path):
    out = await approval.request(tmp_path, PREP, session_key="cron:x", deps=ex.ExecDeps(),
                                 judge=_judge_safe)
    assert out.status == "applied" and RUNS == [{"name": "a"}]
    assert out.record["decided_by"] == {"kind": "judge"}
    assert SEEN_APPROVAL == [{"id": out.record["id"], "decided_by": {"kind": "judge"}}]


@pytest.mark.asyncio
async def test_interactive_yes_applies_no_rejects(tmp_path):
    out = await approval.request(tmp_path, PREP, session_key="websocket:s", deps=ex.ExecDeps(),
                                 judge=_judge_caution, ask=_asker("approve"))
    assert out.status == "applied" and len(RUNS) == 1
    out = await approval.request(tmp_path, PREP, session_key="websocket:s2", deps=ex.ExecDeps(),
                                 ask=_asker("reject"))
    assert out.status == "rejected" and len(RUNS) == 1


@pytest.mark.asyncio
async def test_typed_reply_in_chat_records_the_chat_user_not_a_hand_off(tmp_path):
    # No `decide` call is involved here: the loop parses a yes/no typed
    # straight into the chat and resolves the waiter itself, so there is
    # nothing in `_HANDOFF_DECIDED_BY` to consume — the decider is whoever is
    # on the other end of `session_key`.
    out = await approval.request(tmp_path, PREP, session_key="websocket:s9", deps=ex.ExecDeps(),
                                 ask=_asker("approve"))
    assert out.status == "applied"
    assert out.record["decided_by"] == {"kind": "user", "channel": "websocket:s9"}


@pytest.mark.asyncio
async def test_decide_hand_off_to_a_waiting_turn_records_the_real_decider(tmp_path):
    # The turn's `ask` mirrors the production chat asker: it registers a live
    # `pending_answers` waiter and blocks on it, so `decide`'s `resolve` call
    # is what actually delivers the verdict back into `request`.
    session_key = "websocket:s10"

    async def ask(record):
        fut = pa.create(session_key, kind="approval", ref=record["id"])
        return await fut

    task = asyncio.ensure_future(
        approval.request(tmp_path, PREP, session_key=session_key, deps=ex.ExecDeps(), ask=ask))
    await asyncio.sleep(0)
    rid = pa.waiting_ref(session_key)
    assert rid is not None

    handoff = await approval.decide(
        tmp_path, rid, "approve", decided_by={"kind": "operator", "channel": "cli"},
        deps=ex.ExecDeps())
    assert handoff.status == "pending" and "waiting turn" in handoff.message

    result = await task
    assert result.status == "applied" and len(RUNS) == 1
    assert result.record["decided_by"] == {"kind": "operator", "channel": "cli"}


@pytest.mark.asyncio
async def test_no_answer_or_autonomous_leaves_it_pending_without_duplicates(tmp_path):
    a = await approval.request(tmp_path, PREP, session_key="websocket:s", deps=ex.ExecDeps(),
                               ask=_asker(None))
    b = await approval.request(tmp_path, PREP, session_key="websocket:s", deps=ex.ExecDeps(),
                               ask=_asker(None))
    assert a.status == b.status == "pending"
    assert a.record["id"] == b.record["id"]           # replay does not duplicate
    c = await approval.request(tmp_path, PREP, session_key="cron:x", deps=ex.ExecDeps())
    assert c.status == "pending" and RUNS == []
    assert "do not" in approval.outcome_to_tool_result(c)["message"].lower()


@pytest.mark.asyncio
async def test_decide_later_runs_once_and_refuses_stale(tmp_path):
    out = await approval.request(tmp_path, PREP, session_key="cron:x", deps=ex.ExecDeps())
    rid = out.record["id"]
    first = await approval.decide(tmp_path, rid, "approve", decided_by={"kind": "user"},
                                  deps=ex.ExecDeps())
    again = await approval.decide(tmp_path, rid, "approve", decided_by={"kind": "user"},
                                  deps=ex.ExecDeps())
    assert first.status == "applied" and len(RUNS) == 1
    assert again.status == "refused" and "already" in again.message
    other = await approval.request(tmp_path, PREP, session_key="cron:y", deps=ex.ExecDeps())
    STATE["hash"] = "changed"
    stale = await approval.decide(tmp_path, other.record["id"], "approve",
                                  decided_by={"kind": "user"}, deps=ex.ExecDeps())
    assert stale.status == "stale" and len(RUNS) == 1


@pytest.mark.asyncio
async def test_decide_on_an_expired_pending_record_refuses_and_marks_it_expired(tmp_path):
    from datetime import datetime, timedelta, timezone

    out = await approval.request(tmp_path, PREP, session_key="cron:z", deps=ex.ExecDeps())
    rid = out.record["id"]
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    st.transition(tmp_path, rid, expect=("pending",), to="pending", expires_at=past)

    result = await approval.decide(tmp_path, rid, "approve", decided_by={"kind": "user"},
                                   deps=ex.ExecDeps())
    assert result.status == "stale" and "expired" in result.message
    assert st.get(tmp_path, rid)["status"] == "expired"
    assert RUNS == []

    # Consistent with any other terminal status: a second decision is refused
    # as "already decided", not re-expired.
    again = await approval.decide(tmp_path, rid, "approve", decided_by={"kind": "user"},
                                  deps=ex.ExecDeps())
    assert again.status == "refused" and "expired" in again.message


@pytest.mark.asyncio
async def test_legacy_record_cannot_be_approved(tmp_path):
    (tmp_path / ".approvals" / "skills").mkdir(parents=True)
    (tmp_path / ".approvals" / "skills" / "aaaaaaaaaaaa.json").write_text(
        '{"id":"aaaaaaaaaaaa","subsystem":"skills","summary":"x","status":"pending"}')
    out = await approval.decide(tmp_path, "aaaaaaaaaaaa", "approve",
                                decided_by={"kind": "user"}, deps=ex.ExecDeps())
    assert out.status == "refused" and "legacy" in out.message


def test_unified_and_extra_chat_channels_are_interactive():
    for key in ("unified:default", "feishu:x", "dingtalk:x", "wecom:x", "weixin:x", "qq:x"):
        assert approval.is_interactive(key)
    assert not approval.is_interactive("tui:x")
    assert not approval.is_interactive("cron:x")


@pytest.mark.asyncio
async def test_cancelled_during_execute_leaves_record_failed_and_propagates(tmp_path, monkeypatch):
    async def _cancel_run(ws, payload, deps):
        raise asyncio.CancelledError()

    monkeypatch.setitem(ex._REGISTRY, "skill_edit",
                        (lambda ws, payload: STATE["hash"], _cancel_run))
    out = await approval.request(tmp_path, PREP, session_key="cron:x", deps=ex.ExecDeps())
    rid = out.record["id"]
    with pytest.raises(asyncio.CancelledError):
        await approval.decide(tmp_path, rid, "approve", decided_by={"kind": "user"},
                              deps=ex.ExecDeps())
    rec = st.get(tmp_path, rid)
    assert rec["status"] == "failed"
    assert rec["result"] == {"error": "cancelled"}


@pytest.mark.asyncio
async def test_unknown_answer_leaves_it_pending_and_runs_nothing(tmp_path):
    # A fail-open bug once let anything other than "reject" through as an
    # approval; only "approve" may run it.
    out = await approval.request(tmp_path, PREP, session_key="websocket:s", deps=ex.ExecDeps(),
                                 ask=_asker("yes"))
    assert out.status == "pending" and RUNS == []


@pytest.mark.asyncio
async def test_hash_fn_raising_any_exception_leaves_the_record_failed(tmp_path, monkeypatch):
    def _boom_hash(ws, payload):
        raise OSError("disk gone")

    monkeypatch.setitem(ex._REGISTRY, "skill_edit", (_boom_hash, _run))
    out = await approval.request(tmp_path, PREP, session_key="cron:x", deps=ex.ExecDeps())
    rid = out.record["id"]
    result = await approval.decide(tmp_path, rid, "approve", decided_by={"kind": "user"},
                                   deps=ex.ExecDeps())
    assert result.status == "failed"
    assert st.get(tmp_path, rid)["status"] == "failed"


@pytest.mark.asyncio
async def test_judge_safe_approves_the_existing_pending_record_not_a_duplicate(tmp_path):
    pending = await approval.request(tmp_path, PREP, session_key="websocket:s", deps=ex.ExecDeps(),
                                     ask=_asker(None))
    assert pending.status == "pending"
    out = await approval.request(tmp_path, PREP, session_key="websocket:s", deps=ex.ExecDeps(),
                                 judge=_judge_safe)
    assert out.status == "applied" and RUNS == [{"name": "a"}]
    assert out.record["id"] == pending.record["id"]
    assert len(st.list_records(tmp_path, include_legacy=False)) == 1


MCP_PREP = ex.Prepared(kind="mcp_change", summary="update mcp 'x'", detail={},
                       payload={"name": "x"}, change_hash="h1")


@pytest.mark.asyncio
async def test_judge_never_clears_mcp_change(tmp_path):
    out = await approval.request(tmp_path, MCP_PREP, session_key="cron:x", deps=ex.ExecDeps(),
                                 judge=_judge_safe)
    assert out.status == "pending" and RUNS == []


def test_ensure_loaded_reraises_a_dependency_import_error(monkeypatch):
    # A ModuleNotFoundError for something the kind module itself imports must
    # not be mistaken for the kind module not existing yet.
    def _fake_import(name):
        if name == ex._KIND_MODULES[0]:
            raise ModuleNotFoundError("no module named 'some_missing_dep'",
                                      name="some_missing_dep")
        raise ModuleNotFoundError(name=name)

    monkeypatch.setattr(ex.importlib, "import_module", _fake_import)
    monkeypatch.delitem(ex._REGISTRY, "skill_edit", raising=False)
    with pytest.raises(ModuleNotFoundError):
        ex._ensure_loaded("skill_edit")
