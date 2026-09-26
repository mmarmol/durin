"""GET /api/v1/pending — everything that waits on a person, in one list.

The route only aggregates: each source's own listing function stays the source
of truth, and each item carries that source's record as ``data`` (the shape its
own route returns, which the webui's existing card for it renders) plus how to
resolve it. Approval records are expired and pruned before they are listed, a
workflow run started by an automation is left to the automations inbox, and a
source is shown only to a principal holding the scope its own listing route
requires.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from durin.agent import approval_store as st
from durin.agent import skill_suggestions as sg
from durin.automations import run_log as automation_runs
from durin.memory.refine_dream import add_flagged
from durin.service import pending as pending_mod
from durin.service.catalog import build_catalog_registry
from durin.service.pending import PendingQuery, PendingService, collect_pending
from durin.service.principal import Principal, Scope
from durin.service.types import ForbiddenError
from durin.workflow import run_log as workflow_runs

ADMIN = Principal.webui("tok", {Scope.ADMIN.value})


# -- seeding: one real record per source ----------------------------------------


def _approval(ws: Path, kind: str = "skill_install", session: str = "cron:nightly") -> dict:
    return st.create(ws, kind=kind, summary=f"{kind} 'demo'", detail={"verdict": "caution"},
                     payload={"name": "demo"}, change_hash="h", session_key=session,
                     context="autonomous")


def _quarantine(ws: Path, name: str = "imported") -> None:
    qdir = ws / ".durin" / "import-quarantine" / name
    qdir.mkdir(parents=True)
    (qdir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n",
                                   encoding="utf-8")
    (qdir / ".scan.json").write_text(
        json.dumps({"source": f"github:acme/{name}", "verdict": "caution", "findings": []}),
        encoding="utf-8")


def _suggestion(ws: Path) -> dict:
    # A skill with provenance, so listing the quarantine does not sweep it in.
    (ws / "skills" / "x").mkdir(parents=True)
    (ws / "skills" / "x" / "SKILL.md").write_text(
        "---\nname: x\ndescription: d\nmetadata:\n  durin:\n    mode: manual\n"
        "    provenance:\n      source: test\n      verdict: safe\n---\nold body\n",
        encoding="utf-8")
    return sg.add_suggestion(ws, {"type": "evolve", "name": "x", "old": "old body",
                                  "new": "new body", "rationale": "clearer steps"})


def _paused_automation_run(ws: Path, name: str = "invoice-reminder", run_id: str = "arun1") -> None:
    automation_runs.start_run(ws, name, run_id, cause={"kind": "schedule", "excerpt": ""})
    automation_runs.update_run(ws, name, run_id, status="paused", ask_kind="approval",
                               ask="Send the reminder?", proposal="Send the reminder?")


def _needs_input_run(ws: Path, name: str, run_id: str, *, origin: str) -> None:
    now = time.time()
    path = workflow_runs._record_path(ws, name, run_id)
    path.write_text(json.dumps({
        "schema": workflow_runs.SCHEMA, "run_id": run_id, "workflow": name,
        "status": "needs_input", "root_session_key": origin, "started_at": now - 60,
        "finished_at": now, "ts": now, "task": "triage the inbox",
        "needs_input_node": "ask", "ask_kind": "question",
        "final_output": "Which mailbox?", "runs": [],
    }), encoding="utf-8")


def _flagged_pair(ws: Path) -> None:
    add_flagged(ws, "person:ana", "person:ana-lopez", verdict="same", confidence=70,
                reasoning="same email address")


def _seed_all(ws: Path) -> None:
    _approval(ws)
    _quarantine(ws)
    _suggestion(ws)
    _paused_automation_run(ws)
    _needs_input_run(ws, "triage", "wrun1", origin="websocket:chat-1")
    _flagged_pair(ws)


def _sources(result) -> list[str]:
    return sorted(item.source for item in result.items)


# -- the list ------------------------------------------------------------------


def test_the_list_merges_all_six_sources(tmp_path):
    _seed_all(tmp_path)

    result = collect_pending(tmp_path, ADMIN)

    assert _sources(result) == [
        "approval", "automation_run", "flagged_pair",
        "skill_quarantine", "skill_suggestion", "workflow_run",
    ]
    assert result.count == 6 and result.errors == []


@pytest.mark.asyncio
async def test_the_route_serves_the_same_list(tmp_path):
    _seed_all(tmp_path)
    service = PendingService(workspace_resolver=lambda: tmp_path)

    result = await service.list(PendingQuery(), ADMIN)

    assert result.count == 6


def test_a_workflow_run_started_by_an_automation_is_left_to_automations(tmp_path):
    _needs_input_run(tmp_path, "triage", "mine", origin="websocket:chat-1")
    _needs_input_run(tmp_path, "triage", "theirs", origin="automation:invoice-reminder")

    result = collect_pending(tmp_path, ADMIN)

    assert [item.id for item in result.items] == ["mine"]


def test_expired_approval_records_do_not_show_as_pending(tmp_path):
    fresh = _approval(tmp_path)
    stale = _approval(tmp_path, kind="skill_edit")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    st.transition(tmp_path, stale["id"], expect=("pending",), to="pending", expires_at=past)

    result = collect_pending(tmp_path, ADMIN)

    assert [item.id for item in result.items] == [fresh["id"]]
    assert st.get(tmp_path, stale["id"])["status"] == "expired"


def test_a_pending_install_lists_once_as_its_approval(tmp_path):
    # The request names the quarantined import it would install: the approval
    # item represents it, and the import is not listed a second time.
    _quarantine(tmp_path, "demo")
    rec = st.create(tmp_path, kind="skill_install", summary="install skill 'demo'",
                    detail={}, payload={"quarantine": "demo"}, change_hash="h",
                    session_key="cron:x", context="autonomous")

    result = collect_pending(tmp_path, ADMIN)

    assert [(item.source, item.id) for item in result.items] == [("approval", rec["id"])]
    assert result.count == 1


def test_a_quarantined_import_with_no_request_still_lists(tmp_path):
    _quarantine(tmp_path, "demo")
    st.create(tmp_path, kind="skill_install", summary="install skill 'other'",
              detail={}, payload={"quarantine": "other"}, change_hash="h",
              session_key="cron:x", context="autonomous")

    sources = sorted((item.source, item.id) for item in collect_pending(tmp_path, ADMIN).items)

    assert ("skill_quarantine", "demo") in sources


def test_rejecting_a_pending_install_leaves_nothing_listed(tmp_path):
    import asyncio

    from durin.agent import approval
    from durin.agent import approval_kinds_skills as kinds
    from durin.agent.approval_executors import ExecDeps

    kinds.register_all()
    _quarantine(tmp_path, "demo")
    rec = st.create(tmp_path, kind="skill_install", summary="install skill 'demo'",
                    detail={}, payload={"quarantine": "demo"}, change_hash="h",
                    session_key="cron:x", context="autonomous")

    asyncio.run(approval.decide(tmp_path, rec["id"], "reject",
                                decided_by={"kind": "user", "channel": "webui"},
                                deps=ExecDeps()))

    assert collect_pending(tmp_path, ADMIN).items == []


def test_a_legacy_record_is_left_to_the_cli(tmp_path):
    # It carries no payload, so it can only be discarded (`durin approvals
    # discard`), never decided: the Pending page has nothing to offer for it.
    legacy = tmp_path / ".approvals" / "skills"
    legacy.mkdir(parents=True)
    (legacy / "aaaaaaaaaaaa.json").write_text(
        '{"id":"aaaaaaaaaaaa","subsystem":"skills","summary":"x","status":"pending"}')

    assert collect_pending(tmp_path, ADMIN).items == []


def test_each_item_carries_its_sources_record_and_how_to_resolve_it(tmp_path):
    _seed_all(tmp_path)

    items = {item.source: item for item in collect_pending(tmp_path, ADMIN).items}

    approval = items["approval"]
    assert approval.resolve.form == "approval"
    assert [(a.name, a.method, a.path, a.body) for a in approval.resolve.actions] == [
        ("approve", "POST", f"/api/v1/approvals/{approval.id}/decision",
         {"decision": "approve"}),
        ("reject", "POST", f"/api/v1/approvals/{approval.id}/decision",
         {"decision": "reject"}),
    ]
    assert approval.data["approval_id"] == approval.id
    assert approval.data["detail"] == {"verdict": "caution"}
    assert "payload" not in approval.data

    quarantine = items["skill_quarantine"]
    assert quarantine.id == "imported" and quarantine.data["verdict"] == "caution"
    assert [a.path for a in quarantine.resolve.actions] == [
        "/api/v1/skills/imported/approve", "/api/v1/skills/imported/quarantine"]
    assert quarantine.created_at is not None

    run = items["automation_run"]
    assert run.data["ask"] == "Send the reminder?" and run.kind == "approval"
    assert run.resolve.actions[0].path == (
        "/api/v1/automations/invoice-reminder/runs/arun1/answer")

    workflow = items["workflow_run"]
    assert workflow.data["questions"] == "Which mailbox?"
    assert workflow.resolve.actions[0].body == {"resume_run_id": "wrun1"}

    pair = items["flagged_pair"]
    assert pair.data["ref_a"] == "person:ana" and pair.data["ref_b"] == "person:ana-lopez"
    assert pair.resolve.actions[0].body == {"ref_a": "person:ana", "ref_b": "person:ana-lopez"}

    suggestion = items["skill_suggestion"]
    assert suggestion.data["skill"] == "x" and suggestion.data["reason"] == "clearer steps"
    assert [a.name for a in suggestion.resolve.actions] == ["accept", "reject"]

    for item in items.values():
        assert item.title and item.created_at


# -- who sees what -----------------------------------------------------------


def test_a_source_shows_only_with_its_read_scope(tmp_path):
    _seed_all(tmp_path)
    _approval(tmp_path, kind="mcp_change")

    workflows_only = collect_pending(
        tmp_path, Principal.remote("t", {Scope.WORKFLOWS_READ.value}))
    skills_only = collect_pending(tmp_path, Principal.remote("t", {Scope.SKILLS_READ.value}))

    assert _sources(workflows_only) == ["workflow_run"]
    # A skill-kind request shows with skills:read; the MCP change does not.
    assert _sources(skills_only) == ["approval", "skill_quarantine", "skill_suggestion"]
    assert [i.kind for i in skills_only.items if i.source == "approval"] == ["skill_install"]


def test_a_principal_that_can_read_no_source_is_refused(tmp_path):
    with pytest.raises(ForbiddenError):
        collect_pending(tmp_path, Principal.remote("t", {Scope.CHAT_WRITE.value}))


def test_each_sources_scope_is_the_one_its_own_listing_route_requires():
    routes = {(b.spec.verb, b.spec.path): b.spec.scope for b in build_catalog_registry().routes}

    for source, (path, scope) in pending_mod.SOURCE_ROUTES.items():
        assert routes[("GET", path)] == scope.value, source


def test_a_failing_source_is_reported_and_the_others_still_listed(tmp_path, monkeypatch):
    _seed_all(tmp_path)

    def _broken(_ws):
        raise OSError("disk unreadable")

    monkeypatch.setattr(pending_mod, "_flagged_pair_items", _broken)

    result = collect_pending(tmp_path, ADMIN)

    assert "flagged_pair" not in _sources(result) and result.count == 5
    assert [(e.source, e.detail) for e in result.errors] == [("flagged_pair", "disk unreadable")]
