"""install_imported_skill records who authorized a gated install, and
install_gate reports the same decision the install enforces."""
import asyncio
import json
from pathlib import Path

import pytest

from durin.agent import skills_store as ss
from durin.agent.skills_frontmatter import split_frontmatter
from durin.agent.skills_import import SkillImportRefused, install_gate, install_imported_skill


def _quar(ws: Path, name: str, body: str = "ok\n", scripts: dict | None = None) -> Path:
    q = ws / ".durin" / "import-quarantine" / name
    q.mkdir(parents=True)
    (q / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}")
    for fn, content in (scripts or {}).items():
        (q / "scripts").mkdir(exist_ok=True)
        (q / "scripts" / fn).write_text(content)
    return q


def _prov(ws: Path, name: str) -> dict:
    data, _ = split_frontmatter((ws / "skills" / name / "SKILL.md").read_text())
    return data["metadata"]["durin"]["provenance"]


def test_approved_install_records_the_approval(tmp_path):
    q = _quar(tmp_path, "tool", scripts={"run.sh": "echo hi\n"})
    res = install_imported_skill(tmp_path, q, source="github:x/y", allowlist=[],
                                 confirmed=True, approval_id="abc123abc123",
                                 approved_by="user")
    assert res["ok"]
    prov = _prov(tmp_path, "tool")
    assert prov["approval_id"] == "abc123abc123"
    assert prov["approved_by"] == "user"
    assert "confirmed" not in prov
    audit = [json.loads(line) for line in
             (tmp_path / ".durin" / "import-audit.log").read_text().splitlines()]
    assert audit[-1]["approval_id"] == "abc123abc123"
    assert audit[-1]["approved_by"] == "user"
    assert "confirmed" not in audit[-1]
    msg = ss._store(tmp_path).log(max_entries=1)[0].message
    assert "Approved-by: user" in msg and "Approval: abc123abc123" in msg


def test_ungated_install_records_no_approver(tmp_path):
    q = _quar(tmp_path, "ok")
    install_imported_skill(tmp_path, q, source="github:x/y", allowlist=["github:x/"])
    prov = _prov(tmp_path, "ok")
    assert prov["approval_id"] is None and prov["approved_by"] is None
    assert "Approved-by" not in ss._store(tmp_path).log(max_entries=1)[0].message


def test_install_gate_matches_what_install_enforces(tmp_path):
    q = _quar(tmp_path, "tool", scripts={"run.sh": "echo hi\n"})
    gate = install_gate(q, source="github:x/y", allowlist=["github:x/"])
    assert gate["valid"] and gate["action"] == "confirm" and gate["carries_code"]
    assert gate["code_artifacts"] == ["scripts/run.sh"]
    with pytest.raises(SkillImportRefused) as e:
        install_imported_skill(tmp_path, q, source="github:x/y", allowlist=["github:x/"])
    assert e.value.action == gate["action"]


def test_install_gate_raises_the_verdict_from_cached_judge_findings(tmp_path):
    q = _quar(tmp_path, "ok")
    (q / ".scan.json").write_text(json.dumps({
        "source": "github:x/y", "verdict": "caution",
        "findings": [{"category": "llm:intent", "severity": "caution",
                      "where": "SKILL.md", "detail": "asks for broad file access"}]}))
    gate = install_gate(q, source="github:x/y", allowlist=["github:x/"])
    assert gate["verdict"] == "caution" and gate["action"] == "confirm"
    assert any(f["category"] == "llm:intent" for f in gate["findings"])


def test_install_gate_reports_an_invalid_skill(tmp_path):
    q = tmp_path / "q" / "bad"
    q.mkdir(parents=True)
    (q / "SKILL.md").write_text("---\nname: bad\n---\nno description\n")
    gate = install_gate(q, source="x", allowlist=[])
    assert gate["valid"] is False and gate["action"] == "invalid"
    assert any("description" in err for err in gate["errors"])


def test_web_approve_records_the_user(tmp_path):
    _quar(tmp_path, "tool", scripts={"run.sh": "echo hi\n"})
    status, res = asyncio.run(ss.web_skill_approve(
        tmp_path, "tool", confirm=True, override=False,
        decided_by={"kind": "user", "channel": "webui"}))
    assert status == 200 and res["ok"]
    assert _prov(tmp_path, "tool")["approved_by"] == "user"
