"""`durin approvals` over the approval store (list/approve/reject/discard).

Approve/reject must refuse outside an interactive terminal: the agent's exec
tool runs commands with stdin as a pipe, and a model that could pipe
`durin approvals approve <id>` into a shell would be approving its own
request. `_stdin_is_interactive` is the same TTY probe `durin onboard`
already uses, factored out precisely so tests can monkeypatch it instead of
fighting the test runner's piped stdin.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from durin.agent import approval_kinds_skills as kinds
from durin.agent import approval_store as st
from durin.agent import skills_store as ss
from durin.cli.commands import app

runner = CliRunner()


def _skill(ws, name: str, body: str, mode: str = "manual"):
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\nmetadata:\n  durin:\n    mode: {mode}\n---\n{body}",
        encoding="utf-8")
    return d


def _prep_edit(ws, name: str, old: str, new: str, file: str = "SKILL.md"):
    plan = ss.plan_skill_edit(ws, name, old=old, new=new, rationale="r", file=file)
    scan = ss.scan_skill_write(plan["skill_dir"], {file: plan["after"]})
    return kinds.prepare_skill_edit(
        ws, name, old=old, new=new, rationale="r", file=file,
        attribution=ss.Attribution(actor="agent", session="cron:nightly"),
        plan=plan, scan=scan)


def test_list_approve_and_discard(tmp_path, monkeypatch):
    # approve/reject need a real terminal (ruling X4); patch the TTY probe so
    # this test can exercise the decision itself, not the gate in front of it.
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    monkeypatch.setattr("durin.cli.commands._stdin_is_interactive", lambda: True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    rec = st.create(ws, kind="skill_edit", summary="edit skill 'a'", detail={},
                    payload={}, change_hash="h", session_key="cron:x", context="autonomous")
    out = runner.invoke(app, ["approvals"])
    assert out.exit_code == 0 and rec["id"] in out.output and "edit skill 'a'" in out.output
    out = runner.invoke(app, ["approvals", "reject", rec["id"]])
    assert out.exit_code == 0 and st.get(ws, rec["id"])["status"] == "rejected"
    out = runner.invoke(app, ["approvals", "discard", rec["id"]])
    assert out.exit_code == 0 and st.get(ws, rec["id"]) is None


def test_list_default_hides_resolved_and_all_shows_them(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    ws = tmp_path / "workspace"
    ws.mkdir()
    rec = st.create(ws, kind="skill_edit", summary="edit skill 'b'", detail={},
                    payload={}, change_hash="h", session_key="cron:x", context="autonomous")
    st.transition(ws, rec["id"], expect=("pending",), to="rejected",
                  decided_by={"kind": "operator", "channel": "cli"})

    default = runner.invoke(app, ["approvals"])
    assert default.exit_code == 0
    assert rec["id"] not in default.output
    assert "No pending approvals" in default.output

    listed = runner.invoke(app, ["approvals", "list"])
    assert listed.exit_code == 0
    assert rec["id"] not in listed.output

    everything = runner.invoke(app, ["approvals", "--all"])
    assert everything.exit_code == 0
    assert rec["id"] in everything.output
    assert "rejected" in everything.output


def test_approve_without_tty_refuses_and_changes_nothing(tmp_path, monkeypatch):
    # No monkeypatch of _stdin_is_interactive here: the CliRunner's stdin is
    # a non-tty stream by construction, so this exercises the real refusal.
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    ws = tmp_path / "workspace"
    ws.mkdir()
    rec = st.create(ws, kind="skill_edit", summary="edit skill 'c'", detail={},
                    payload={}, change_hash="h", session_key="cron:x", context="autonomous")

    out = runner.invoke(app, ["approvals", "approve", rec["id"]])
    assert out.exit_code == 1
    assert "approving requires an interactive terminal" in out.output
    assert st.get(ws, rec["id"])["status"] == "pending"


def test_reject_with_tty_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    monkeypatch.setattr("durin.cli.commands._stdin_is_interactive", lambda: True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    rec = st.create(ws, kind="skill_edit", summary="edit skill 'd'", detail={},
                    payload={}, change_hash="h", session_key="cron:x", context="autonomous")

    out = runner.invoke(app, ["approvals", "reject", rec["id"]])
    assert out.exit_code == 0
    assert st.get(ws, rec["id"])["status"] == "rejected"


def test_approve_a_request_whose_target_changed_is_stale(tmp_path, monkeypatch):
    # A real skill_edit record, hashed against the file as it was when filed.
    # The file changes before it's decided, so the executor's re-hash must
    # not match: approving must refuse the run, not apply a stale review.
    kinds.register_all()
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    monkeypatch.setattr("durin.cli.commands._stdin_is_interactive", lambda: True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    skill_dir = _skill(ws, "mine", "step one\n")
    prep = _prep_edit(ws, "mine", "step one", "step two")
    rec = st.create(ws, kind=prep.kind, summary=prep.summary, detail=prep.detail,
                    payload=prep.payload, change_hash=prep.change_hash,
                    session_key="cron:nightly", context="autonomous")

    (skill_dir / "SKILL.md").write_text(
        (skill_dir / "SKILL.md").read_text() + "more\n", encoding="utf-8")

    out = runner.invoke(app, ["approvals", "approve", rec["id"]])
    assert out.exit_code == 1
    assert st.get(ws, rec["id"])["status"] == "stale"
    assert "step two" not in (skill_dir / "SKILL.md").read_text()


def test_approve_applies_a_real_edit(tmp_path, monkeypatch):
    kinds.register_all()
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    monkeypatch.setattr("durin.cli.commands._stdin_is_interactive", lambda: True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    skill_dir = _skill(ws, "mine", "step one\n")
    prep = _prep_edit(ws, "mine", "step one", "step two")
    rec = st.create(ws, kind=prep.kind, summary=prep.summary, detail=prep.detail,
                    payload=prep.payload, change_hash=prep.change_hash,
                    session_key="cron:nightly", context="autonomous")

    out = runner.invoke(app, ["approvals", "approve", rec["id"]])
    assert out.exit_code == 0, out.output
    assert st.get(ws, rec["id"])["status"] == "applied"
    assert "step two" in (skill_dir / "SKILL.md").read_text()
    msg = ss._store(ws).log(max_entries=1)[0].message
    assert "Approved-by: operator" in msg


def test_list_does_not_show_a_pending_record_past_its_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    ws = tmp_path / "workspace"
    ws.mkdir()
    rec = st.create(ws, kind="skill_edit", summary="edit skill 'e'", detail={},
                    payload={}, change_hash="h", session_key="cron:x", context="autonomous")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    st.transition(ws, rec["id"], expect=("pending",), to="pending", expires_at=past)

    out = runner.invoke(app, ["approvals"])
    assert out.exit_code == 0
    assert rec["id"] not in out.output
    assert "No pending approvals" in out.output
    assert st.get(ws, rec["id"])["status"] == "expired"


def test_list_all_prunes_a_resolved_record_past_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    ws = tmp_path / "workspace"
    ws.mkdir()
    rec = st.create(ws, kind="skill_edit", summary="edit skill 'f'", detail={},
                    payload={}, change_hash="h", session_key="cron:x", context="autonomous")
    st.transition(ws, rec["id"], expect=("pending",), to="rejected",
                  decided_by={"kind": "operator", "channel": "cli"})
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    st.transition(ws, rec["id"], expect=("rejected",), to="rejected", decided_at=old)

    out = runner.invoke(app, ["approvals", "--all"])
    assert out.exit_code == 0
    assert rec["id"] not in out.output
    assert st.get(ws, rec["id"]) is None


def test_discard_missing_id_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    ws = tmp_path / "workspace"
    ws.mkdir()
    out = runner.invoke(app, ["approvals", "discard", "nope"])
    assert out.exit_code == 1


_DEPS_SPEC = [{"kind": "pip", "value": "requests", "command": "pip install requests",
               "needs_privileges": False}]


def _deps_record(ws, monkeypatch):
    """A real skill_deps record, as the skill_install_deps tool files it in a
    context with no person (a cron run)."""
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs",
                        lambda _d: list(_DEPS_SPEC))
    kinds.register_all()
    p = kinds.prepare_skill_deps(ws, "demo", list(_DEPS_SPEC))
    return st.create(ws, kind=p.kind, summary=p.summary, detail=p.detail, payload=p.payload,
                     change_hash=p.change_hash, session_key="cron:x", context="autonomous")


def test_approve_skill_deps_runs_the_install_through_an_exec_runner(tmp_path, monkeypatch):
    """The CLI builds the exec tool the gateway builds, from the loaded config,
    and runs the install through its non-asking runner — the same guards
    (deny rules, the hard floor, the workspace boundary) apply."""
    from durin.agent.tools.shell import ExecTool

    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    monkeypatch.setattr("durin.cli.commands._stdin_is_interactive", lambda: True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    rec = _deps_record(ws, monkeypatch)
    calls = []

    async def _run(self, command, working_dir=None, timeout=None, background=False, *,
                   approved_rules=frozenset(), ask=False):
        calls.append({"command": command, "ask": ask, "working_dir": self.working_dir})
        return "Successfully installed requests\n\nExit code: 0"

    monkeypatch.setattr(ExecTool, "_run", _run)

    out = runner.invoke(app, ["approvals", "approve", rec["id"]])
    assert out.exit_code == 0, out.output
    assert calls == [{"command": "pip install requests", "ask": False,
                      "working_dir": str(ws)}]
    stored = st.get(ws, rec["id"])
    assert stored["status"] == "applied"
    assert stored["decided_by"] == {"kind": "operator", "channel": "cli"}


def test_approve_refuses_before_touching_the_record_when_no_runner_can_be_built(
    tmp_path, monkeypatch,
):
    from durin.agent.tools.shell import ExecTool

    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    monkeypatch.setattr("durin.cli.commands._stdin_is_interactive", lambda: True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    rec = _deps_record(ws, monkeypatch)

    def _broken(cls, ctx):
        raise RuntimeError("bad exec config")

    monkeypatch.setattr(ExecTool, "create", classmethod(_broken))

    out = runner.invoke(app, ["approvals", "approve", rec["id"]])
    assert out.exit_code == 1
    assert "bad exec config" in out.output
    stored = st.get(ws, rec["id"])
    assert stored["status"] == "pending" and stored["decided_by"] is None


def test_approve_of_an_exec_request_is_refused_and_leaves_it_pending(tmp_path, monkeypatch):
    from durin.agent import approval_kinds_exec as kx

    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    monkeypatch.setattr("durin.cli.commands._stdin_is_interactive", lambda: True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    p = kx.prepare(command="rm -rf build", cwd=str(ws), rules=(r"\brm\s+-[rf]{1,2}\b",),
                   session_key="websocket:c1", timeout=None, background=False)
    rec = st.create(ws, kind=p.kind, summary=p.summary, detail=p.detail, payload=p.payload,
                    change_hash=p.change_hash, session_key="websocket:c1",
                    context="interactive")

    out = runner.invoke(app, ["approvals", "approve", rec["id"]])
    assert out.exit_code == 1
    assert "only be approved in the chat that asked" in out.output
    assert st.get(ws, rec["id"])["status"] == "pending"


def test_approve_prints_the_executor_note(tmp_path, monkeypatch):
    """With no live gateway handle the MCP change is saved to config only;
    the person at the terminal is told when it takes effect."""
    from durin.agent import approval_kinds_mcp as mk

    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    monkeypatch.setattr("durin.cli.commands._stdin_is_interactive", lambda: True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    p = mk.prepare_upsert("add", "fs", {"command": "npx", "args": ["-y", "@x/fs"]})
    rec = st.create(ws, kind=p.kind, summary=p.summary, detail=p.detail, payload=p.payload,
                    change_hash=p.change_hash, session_key="cron:x", context="autonomous")

    out = runner.invoke(app, ["approvals", "approve", rec["id"]])
    assert out.exit_code == 0, out.output
    assert st.get(ws, rec["id"])["status"] == "applied"
    assert "Done:" in out.output
    assert "restarts" in out.output and "reconnected" in out.output
