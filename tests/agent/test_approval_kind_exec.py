"""exec_command approvals: bound to command, cwd and session; run only in-turn."""
from __future__ import annotations

import pytest

from durin.agent import approval, approval_store
from durin.agent import approval_executors as ex
from durin.agent import approval_kinds_exec as kx

RM_RULE = r"\brm\s+-[rf]{1,2}\b"


def _prep(**over):
    args = dict(command="rm -rf build", cwd="/w", rules=(RM_RULE,),
                session_key="websocket:s", timeout=None, background=False)
    args.update(over)
    return kx.prepare(**args)


def test_prepare_records_what_the_person_reviews():
    p = _prep()
    assert p.kind == "exec_command"
    assert p.summary == "run `rm -rf build`"
    assert p.detail == {"command": "rm -rf build", "cwd": "/w", "rule": RM_RULE}
    assert p.payload["rules"] == [RM_RULE]
    assert p.payload["session_key"] == "websocket:s"


def test_hash_binds_command_cwd_and_session():
    p = _prep()
    assert ex.current_hash("/ws", {"kind": p.kind, "payload": p.payload}) == p.change_hash
    for field, value in (("command", "rm -rf other"), ("cwd", "/x"),
                         ("session_key", "websocket:t")):
        changed = {**p.payload, field: value}
        assert ex.current_hash("/ws", {"kind": p.kind, "payload": changed}) != p.change_hash


@pytest.mark.asyncio
async def test_executor_runs_through_the_turn_runner_past_only_the_recorded_rules():
    calls = []

    async def run(**kw):
        calls.append(kw)
        return "done\nExit code: 0"

    p = _prep()
    out = await ex.execute("/ws", {"kind": p.kind, "payload": p.payload},
                           ex.ExecDeps(exec_run=run))
    assert out == {"output": "done\nExit code: 0"}
    assert calls == [{"command": "rm -rf build", "working_dir": "/w", "timeout": None,
                      "background": False, "approved_rules": frozenset({RM_RULE})}]


@pytest.mark.asyncio
async def test_approving_later_outside_the_turn_fails_without_running(tmp_path):
    p = _prep()
    rec = approval_store.create(tmp_path, kind=p.kind, summary=p.summary, detail=p.detail,
                                payload=p.payload, change_hash=p.change_hash,
                                session_key="websocket:s", context="interactive")
    out = await approval.decide(tmp_path, rec["id"], "approve",
                                decided_by={"kind": "operator", "channel": "cli"},
                                deps=ex.ExecDeps())
    assert out.status == "failed"
    assert "inside the chat turn" in out.message
    assert approval_store.get(tmp_path, rec["id"])["status"] == "failed"
