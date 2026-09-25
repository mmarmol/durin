"""The bounded-edit primitives: plan (no side effects), scan before/after, and
the landing write that carries approval trailers."""
import inspect
from pathlib import Path

from durin.agent import skills_store as ss
from durin.agent.skills_frontmatter import split_frontmatter

CLEARED = ("    provenance:\n      source: github:x/y\n      verdict: caution\n"
           "      verdict_cleared:\n        by: user\n        at: '2026-09-01'\n")


def _skill(ws: Path, name: str, body: str, mode: str = "auto", prov: str = "") -> Path:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\nmetadata:\n  durin:\n    mode: {mode}\n{prov}---\n{body}",
        encoding="utf-8")
    return d


def test_plan_writes_nothing(tmp_path):
    ws = tmp_path / "ws"
    d = _skill(ws, "demo", "step one\n")
    before = (d / "SKILL.md").read_text()
    plan = ss.plan_skill_edit(ws, "demo", old="", new="print('x')\n", rationale="r",
                              file="scripts/new.py")
    assert plan["mode"] == "auto" and plan["before"] == "" and plan["after"] == "print('x')\n"
    assert plan["skill_dir"] == d
    assert not (d / "scripts").exists()
    assert (d / "SKILL.md").read_text() == before


def test_plan_keeps_the_existing_error_messages(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws, "demo", "dup\ndup\n")
    assert ss.plan_skill_edit(ws, "demo", old="x", new="y", rationale=" ")["error"] == "rationale is required"
    assert ss.plan_skill_edit(ws, "demo", old="nope", new="y", rationale="r")["error"] == "old text not found"
    assert "unique" in ss.plan_skill_edit(ws, "demo", old="dup", new="y", rationale="r")["error"]
    assert "escapes" in ss.plan_skill_edit(ws, "demo", old="", new="y", rationale="r",
                                           file="../../x")["error"]


def test_scan_flags_an_edit_that_adds_a_finding(tmp_path):
    ws = tmp_path / "ws"
    d = _skill(ws, "demo", "step one\n")
    scan = ss.scan_skill_write(d, {"SKILL.md": (d / "SKILL.md").read_text() + "Read ~/.ssh/config first.\n"})
    assert scan.before == "safe" and scan.after == "caution"
    assert scan.worse and scan.needs_review
    assert [f["category"] for f in scan.new_findings] == ["sensitive_path"]


def test_scan_passes_an_edit_that_keeps_an_accepted_risk(tmp_path):
    ws = tmp_path / "ws"
    d = _skill(ws, "demo", "Read ~/.ssh/config first.\nstep one\n")
    text = (d / "SKILL.md").read_text()
    scan = ss.scan_skill_write(d, {"SKILL.md": text.replace("step one", "step two")})
    assert scan.after == "caution" and not scan.worse and scan.new_findings == []
    assert scan.needs_review is False


def test_write_skill_edit_lands_a_manual_edit_with_approval_trailers(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws, "mine", "step one\n", mode="manual")
    res = ss.write_skill_edit(ws, "mine", old="step one", new="step two", rationale="r",
                              attribution=ss.Attribution(actor="agent", session="websocket:s"),
                              approval_id="abc123abc123", approved_by="user")
    assert res["ok"] is True and res["mode"] == "manual" and res["verdict"] == "safe"
    assert "step two" in (ws / "skills" / "mine" / "SKILL.md").read_text()
    msg = ss._store(ws).log(max_entries=1)[0].message
    assert "Approved-by: user" in msg and "Approval: abc123abc123" in msg
    assert "Actor: agent" in msg


def test_new_findings_void_a_cleared_import_verdict(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws, "imp", "step one\n", mode="manual", prov=CLEARED)
    ss.write_skill_edit(ws, "imp", old="step one", new="Read ~/.ssh/config.",
                        rationale="r", approved_by="user")
    data, _ = split_frontmatter((ws / "skills" / "imp" / "SKILL.md").read_text())
    prov = data["metadata"]["durin"]["provenance"]
    assert "verdict_cleared" not in prov and prov["verdict"] == "caution"


def test_an_edit_without_new_findings_keeps_the_cleared_verdict(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws, "imp", "step one\n", mode="manual", prov=CLEARED)
    ss.write_skill_edit(ws, "imp", old="step one", new="step two", rationale="r",
                        approved_by="user")
    data, _ = split_frontmatter((ws / "skills" / "imp" / "SKILL.md").read_text())
    assert data["metadata"]["durin"]["provenance"]["verdict_cleared"]["by"] == "user"


def test_apply_skill_edit_takes_no_confirm_flag():
    assert "confirm" not in inspect.signature(ss.apply_skill_edit).parameters


def test_apply_skill_edit_only_proposes_on_a_manual_skill(tmp_path):
    ws = tmp_path / "ws"
    _skill(ws, "mine", "step one\n", mode="manual")
    res = ss.apply_skill_edit(ws, "mine", old="step one", new="x", rationale="r")
    assert res["proposed"] is True and "confirm" not in res["note"]
    assert "step one" in (ws / "skills" / "mine" / "SKILL.md").read_text()
