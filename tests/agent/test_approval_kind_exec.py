"""exec_command approvals: bound to command, cwd and session; run only in-turn,
and never store the literal command (an inline credential must not sit on disk)."""
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


def test_an_inline_bearer_token_is_masked_before_it_touches_the_record(tmp_path):
    command = 'curl -H "Authorization: Bearer abc123" https://api.example.com'
    p = kx.prepare(command=command, cwd="/w", rules=(RM_RULE,),
                   session_key="websocket:s", timeout=None, background=False)
    assert "abc123" not in p.payload["command"]
    assert "abc123" not in p.detail["command"]
    assert "abc123" not in p.summary

    rec = approval_store.create(tmp_path, kind=p.kind, summary=p.summary, detail=p.detail,
                                payload=p.payload, change_hash=p.change_hash,
                                session_key="websocket:s", context="interactive")
    on_disk_files = list((tmp_path / ".approvals").glob("**/*.json"))
    assert on_disk_files, "the record was not written to disk"
    for path in on_disk_files:
        text = path.read_text()
        assert "abc123" not in text
    # sanity: the record really was written and really is the one we made
    assert approval_store.get(tmp_path, rec["id"])["id"] == rec["id"]


@pytest.mark.asyncio
async def test_executor_runs_the_literal_when_the_turn_hands_it_over():
    calls = []

    async def run(**kw):
        calls.append(kw)
        return "done\nExit code: 0"

    p = _prep()
    deps = ex.ExecDeps(exec_run=run, extra={"exec_command": "rm -rf build"})
    out = await ex.execute("/ws", {"kind": p.kind, "payload": p.payload}, deps)
    assert out == {"output": "done\nExit code: 0"}
    assert calls == [{"command": "rm -rf build", "working_dir": "/w", "timeout": None,
                      "background": False, "approved_rules": frozenset({RM_RULE})}]


@pytest.mark.asyncio
async def test_a_literal_that_does_not_redact_to_the_recorded_one_is_refused():
    async def run(**kw):
        raise AssertionError("must not run — the literal does not match what was approved")

    p = _prep()
    deps = ex.ExecDeps(exec_run=run, extra={"exec_command": "rm -rf other"})
    with pytest.raises(ex.ApprovalExecError):
        await ex.execute("/ws", {"kind": p.kind, "payload": p.payload}, deps)


@pytest.mark.asyncio
async def test_no_literal_fails_without_running():
    async def run(**kw):
        raise AssertionError("must not run — there is no literal to run")

    p = _prep()
    deps = ex.ExecDeps(exec_run=run, extra={})
    with pytest.raises(ex.ApprovalExecError, match="inside the chat turn"):
        await ex.execute("/ws", {"kind": p.kind, "payload": p.payload}, deps)


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
