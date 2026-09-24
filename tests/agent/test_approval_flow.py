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
async def test_cancelled_during_execute_leaves_record_failed_and_propagates(tmp_path):
    async def _cancel_run(ws, payload, deps):
        raise asyncio.CancelledError()

    ex._REGISTRY["skill_edit"] = (lambda ws, payload: STATE["hash"], _cancel_run)
    out = await approval.request(tmp_path, PREP, session_key="cron:x", deps=ex.ExecDeps())
    rid = out.record["id"]
    with pytest.raises(asyncio.CancelledError):
        await approval.decide(tmp_path, rid, "approve", decided_by={"kind": "user"},
                              deps=ex.ExecDeps())
    rec = st.get(tmp_path, rid)
    assert rec["status"] == "failed"
