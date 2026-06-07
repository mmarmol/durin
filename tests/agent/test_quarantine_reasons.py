import json
from pathlib import Path

from durin.agent.skills_surface import quarantined_skills as web_quarantine


def _q(tmp_path: Path, name: str, *, verdict="safe", source="github:o/r", scripts=False):
    q = tmp_path / ".durin" / "import-quarantine" / name
    q.mkdir(parents=True)
    (q / "SKILL.md").write_text(
        "---\nname: %s\ndescription: x\n---\nbody\n" % name, encoding="utf-8"
    )
    (q / ".scan.json").write_text(
        json.dumps({"source": source, "verdict": verdict, "findings": []}), encoding="utf-8"
    )
    if scripts:
        (q / "scripts").mkdir()
        (q / "scripts" / "go.sh").write_text("echo hi\n", encoding="utf-8")
    return tmp_path


def _codes(row):
    return {r["code"] for r in row["reasons"]}


def test_untrusted_source_reason(tmp_path, monkeypatch):
    ws = _q(tmp_path, "demo")
    monkeypatch.setattr("durin.agent.skills_store._import_allowlist", lambda: [])
    rows = web_quarantine(ws)
    row = next(r for r in rows if r["name"] == "demo")
    assert row["needs"] == "confirm"
    assert "untrusted_source" in _codes(row)


def test_carries_code_reason(tmp_path, monkeypatch):
    ws = _q(tmp_path, "demo", scripts=True)
    monkeypatch.setattr("durin.agent.skills_store._import_allowlist", lambda: ["github:o/r"])
    rows = web_quarantine(ws)
    row = next(r for r in rows if r["name"] == "demo")
    assert "carries_code" in _codes(row)


def test_dangerous_blocks(tmp_path, monkeypatch):
    ws = _q(tmp_path, "demo", verdict="dangerous")
    monkeypatch.setattr("durin.agent.skills_store._import_allowlist", lambda: ["github:o/r"])
    rows = web_quarantine(ws)
    row = next(r for r in rows if r["name"] == "demo")
    assert row["needs"] == "block"
    assert "verdict_dangerous" in _codes(row)
