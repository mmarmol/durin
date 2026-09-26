"""A pending skill install is one decision, whichever surface decides it.

An install request filed for a person (a ``skill_install`` approval record)
names a quarantined import. Two surfaces can settle that import: the approval
itself (a chat card, the Pending page, ``durin approvals``) and the Skills
triage. Whichever acts first settles both: rejecting the request discards the
quarantined import, and installing or discarding it from the triage closes the
request, so neither is left waiting for a decision already made.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from durin.agent import approval, approval_store
from durin.agent import approval_kinds_skills as kinds
from durin.agent import skills_store as ss
from durin.agent.approval_executors import ExecDeps
from durin.agent.skills_import import install_gate

PERSON = {"kind": "user", "channel": "webui"}


@pytest.fixture(autouse=True)
def _kinds_registered():
    kinds.register_all()
    yield


def _quar(ws: Path, name: str) -> Path:
    q = ws / ".durin" / "import-quarantine" / name
    q.mkdir(parents=True)
    (q / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nok\n")
    (q / "scripts").mkdir()
    (q / "scripts" / "run.sh").write_text("echo hi\n")
    (q / ".scan.json").write_text(json.dumps(
        {"source": f"github:acme/{name}", "verdict": "safe", "findings": []}))
    return q


def _file_install(ws: Path, q: Path, session: str = "cron:nightly") -> dict:
    gate = install_gate(q, source=f"github:acme/{q.name}", allowlist=[])
    prep = kinds.prepare_skill_install(
        ws, q, gate=gate, source=f"github:acme/{q.name}", replace=False,
        attribution=ss.Attribution(actor="import", session=session))
    return approval_store.create(
        ws, kind=prep.kind, summary=prep.summary, detail=prep.detail,
        payload=prep.payload, change_hash=prep.change_hash, session_key=session,
        context="autonomous")


def _audit(ws: Path) -> list[dict]:
    log = ws / ".durin" / "import-audit.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


def test_rejecting_the_request_discards_the_quarantined_import(tmp_path):
    q = _quar(tmp_path, "demo")
    rec = _file_install(tmp_path, q)

    out = asyncio.run(approval.decide(tmp_path, rec["id"], "reject",
                                      decided_by=PERSON, deps=ExecDeps()))

    assert out.status == "rejected"
    assert not q.exists()
    assert _audit(tmp_path)[-1] == {
        "event": "discarded", "name": "demo", "source": "github:acme/demo",
        "verdict": "safe", "approval_id": rec["id"], "decided_by": "user"}


def test_rejecting_one_request_closes_the_others_for_the_same_import(tmp_path):
    q = _quar(tmp_path, "demo")
    first = _file_install(tmp_path, q, session="cron:nightly")
    second = _file_install(tmp_path, q, session="websocket:chat-1")

    asyncio.run(approval.decide(tmp_path, first["id"], "reject",
                                decided_by=PERSON, deps=ExecDeps()))

    other = approval_store.get(tmp_path, second["id"])
    assert other["status"] == "rejected" and other["decided_by"] == PERSON


def test_approving_the_request_installs_as_before(tmp_path):
    q = _quar(tmp_path, "demo")
    rec = _file_install(tmp_path, q)

    out = asyncio.run(approval.decide(tmp_path, rec["id"], "approve",
                                      decided_by=PERSON, deps=ExecDeps()))

    assert out.status == "applied"
    assert (tmp_path / "skills" / "demo" / "SKILL.md").is_file() and not q.exists()


def test_installing_from_the_skills_triage_closes_the_request_as_applied(tmp_path):
    q = _quar(tmp_path, "demo")
    rec = _file_install(tmp_path, q)

    status, payload = asyncio.run(ss.web_skill_approve(
        tmp_path, "demo", confirm=True, override=False, decided_by=PERSON))

    assert status == 200, payload
    closed = approval_store.get(tmp_path, rec["id"])
    assert closed["status"] == "applied"
    assert closed["decided_by"] == PERSON
    assert closed["result"]["name"] == "demo"


def test_discarding_from_the_skills_triage_closes_the_request_as_rejected(tmp_path):
    q = _quar(tmp_path, "demo")
    rec = _file_install(tmp_path, q)

    status, _ = ss.web_skill_reject(tmp_path, "demo", decided_by=PERSON)

    assert status == 200 and not q.exists()
    closed = approval_store.get(tmp_path, rec["id"])
    assert closed["status"] == "rejected" and closed["decided_by"] == PERSON
    assert _audit(tmp_path)[-1]["event"] == "discarded"
    assert _audit(tmp_path)[-1]["decided_by"] == "user"


def test_a_refused_triage_install_leaves_the_request_pending(tmp_path):
    q = _quar(tmp_path, "demo")
    rec = _file_install(tmp_path, q)

    # The import carries code, so the gate wants a confirmation first.
    status, payload = asyncio.run(ss.web_skill_approve(
        tmp_path, "demo", confirm=False, override=False, decided_by=PERSON))

    assert status == 409 and payload["refused"] == "confirm"
    assert approval_store.get(tmp_path, rec["id"])["status"] == "pending"


def test_a_triage_action_leaves_other_imports_requests_alone(tmp_path):
    _quar(tmp_path, "demo")
    other = _file_install(tmp_path, _quar(tmp_path, "other"))

    ss.web_skill_reject(tmp_path, "demo", decided_by=PERSON)

    assert approval_store.get(tmp_path, other["id"])["status"] == "pending"


# -- the real decider, from the route's principal --------------------------------

def _prov(ws: Path, name: str) -> dict:
    from durin.agent.skills_frontmatter import split_frontmatter

    data, _ = split_frontmatter((ws / "skills" / name / "SKILL.md").read_text())
    return data["metadata"]["durin"]["provenance"]


def test_a_triage_install_by_an_api_token_records_the_operator(tmp_path):
    from durin.service.principal import Principal, Scope
    from durin.service.skills import SkillApproveCommand, SkillsService

    q = _quar(tmp_path, "demo")
    rec = _file_install(tmp_path, q)
    token = Principal.remote("tok123", {Scope.SKILLS_WRITE.value})

    asyncio.run(SkillsService(workspace=tmp_path).approve(
        SkillApproveCommand(name="demo", confirm=True), token))

    closed = approval_store.get(tmp_path, rec["id"])
    assert closed["status"] == "applied"
    assert closed["decided_by"] == {"kind": "operator", "channel": "api", "principal": "tok123"}
    assert _prov(tmp_path, "demo")["approved_by"] == "operator"


def test_a_triage_install_by_the_dashboard_records_the_person(tmp_path):
    from durin.service.principal import Principal, Scope
    from durin.service.skills import SkillApproveCommand, SkillsService

    q = _quar(tmp_path, "demo")
    rec = _file_install(tmp_path, q)
    session = Principal.webui("tok-webui", {Scope.ADMIN.value})

    asyncio.run(SkillsService(workspace=tmp_path).approve(
        SkillApproveCommand(name="demo", confirm=True), session))

    assert approval_store.get(tmp_path, rec["id"])["decided_by"] == PERSON
    assert _prov(tmp_path, "demo")["approved_by"] == "user"


def test_a_triage_discard_by_an_api_token_records_the_operator(tmp_path):
    from durin.service.principal import Principal, Scope
    from durin.service.skills import SkillRejectCommand, SkillsService

    q = _quar(tmp_path, "demo")
    rec = _file_install(tmp_path, q)
    token = Principal.remote("tok123", {Scope.SKILLS_WRITE.value})

    asyncio.run(SkillsService(workspace=tmp_path).reject(SkillRejectCommand(name="demo"), token))

    closed = approval_store.get(tmp_path, rec["id"])
    assert closed["status"] == "rejected"
    assert closed["decided_by"] == {"kind": "operator", "channel": "api", "principal": "tok123"}
    assert _audit(tmp_path)[-1]["decided_by"] == "operator"


def test_a_triage_discard_by_the_dashboard_records_the_person(tmp_path):
    from durin.service.principal import Principal, Scope
    from durin.service.skills import SkillRejectCommand, SkillsService

    q = _quar(tmp_path, "demo")
    rec = _file_install(tmp_path, q)
    session = Principal.webui("tok-webui", {Scope.ADMIN.value})

    asyncio.run(SkillsService(workspace=tmp_path).reject(SkillRejectCommand(name="demo"), session))

    assert approval_store.get(tmp_path, rec["id"])["decided_by"] == PERSON
    assert _audit(tmp_path)[-1]["decided_by"] == "user"


def test_decider_of_names_each_kind_of_caller():
    from durin.service.approvals import decider_of
    from durin.service.principal import Principal, Scope

    assert decider_of(Principal.webui("t", {Scope.ADMIN.value})) == PERSON
    assert decider_of(Principal.remote("static", {Scope.ADMIN.value})) == {
        "kind": "operator", "channel": "api", "principal": "static"}
    assert decider_of(Principal.local()) == {"kind": "operator", "channel": "local"}
