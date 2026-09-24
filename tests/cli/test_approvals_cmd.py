"""`durin approvals` over the approval store (list/approve/reject/discard).

Approve/reject must refuse outside an interactive terminal: the agent's exec
tool runs commands with stdin as a pipe, and a model that could pipe
`durin approvals approve <id>` into a shell would be approving its own
request. `_stdin_is_interactive` is the same TTY probe `durin onboard`
already uses, factored out precisely so tests can monkeypatch it instead of
fighting the test runner's piped stdin.
"""
from __future__ import annotations

from typer.testing import CliRunner

from durin.agent import approval_store as st
from durin.cli.commands import app

runner = CliRunner()


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


def test_approve_with_tty_but_no_executor_exits_nonzero(tmp_path, monkeypatch):
    # No approval_kinds_* module is registered for "skill_edit" yet in this
    # branch (later tasks add them), so approving past the TTY gate must
    # still fail loudly rather than silently pretend to succeed.
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    monkeypatch.setattr("durin.cli.commands._stdin_is_interactive", lambda: True)
    ws = tmp_path / "workspace"
    ws.mkdir()
    rec = st.create(ws, kind="skill_edit", summary="edit skill 'e'", detail={},
                    payload={}, change_hash="h", session_key="cron:x", context="autonomous")

    out = runner.invoke(app, ["approvals", "approve", rec["id"]])
    assert out.exit_code == 1
    assert st.get(ws, rec["id"])["status"] == "failed"


def test_discard_missing_id_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    ws = tmp_path / "workspace"
    ws.mkdir()
    out = runner.invoke(app, ["approvals", "discard", "nope"])
    assert out.exit_code == 1
