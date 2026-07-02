import json
from pathlib import Path

from durin.agent import skills_store as ss


def _user_skill(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    prov = ("metadata:\n  durin:\n    provenance:\n"
            '      source: "github:o/r/x"\n      content_hash: "abc"\n')
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n{prov}---\nBody\n", encoding="utf-8"
    )


def test_web_list_returns_200_and_skills(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    status, payload = ss.web_list(ws)
    assert status == 200
    assert any(s["name"] == "mine" for s in payload["skills"])


def test_web_list_skills_carry_verdict_and_status(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    status, payload = ss.web_list(ws)
    assert status == 200
    assert "store_head" in payload
    mine = next(s for s in payload["skills"] if s["name"] == "mine")
    assert mine["status"] == "active"
    assert mine["verdict"] == "safe"
    assert mine["findings"] == []


def test_web_quarantine_returns_pending_list(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    q = ws / ".durin" / "import-quarantine" / "pending"
    q.mkdir(parents=True)
    (q / "SKILL.md").write_text("---\nname: pending\ndescription: d\n---\nhi\n")
    (q / ".scan.json").write_text(
        json.dumps(
            {
                "source": "github:x/y",
                "verdict": "caution",
                "findings": [
                    {
                        "category": "secrets",
                        "severity": "caution",
                        "where": "SKILL.md",
                        "detail": "x",
                    }
                ],
            }
        )
    )
    status, payload = ss.web_quarantine(ws)
    assert status == 200
    out = {s["name"]: s for s in payload["quarantined"]}
    assert out["pending"]["status"] == "quarantined"
    assert out["pending"]["verdict"] == "caution"
    assert out["pending"]["source"] == "github:x/y"


def test_web_get_returns_content_or_404(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    status, payload = ss.web_get(ws, "mine")
    assert status == 200 and "Body" in payload["content"]
    assert ss.web_get(ws, "nope")[0] == 404


def test_web_mode_sets_and_validates(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    assert ss.web_mode(ws, "mine", "auto")[0] == 200
    assert ss.read_mode(ws, "mine") == "auto"
    assert ss.web_mode(ws, "mine", "bogus")[0] == 400


def test_web_save_editable_in_both_modes(tmp_path):
    # auto is not a user lock: the web save endpoint works in either mode.
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    assert ss.web_save(ws, "mine", "---\nname: mine\ndescription: d\n---\nNEW\n")[0] == 200
    ss.web_mode(ws, "mine", "auto")
    assert ss.web_save(ws, "mine", "---\nname: mine\ndescription: d\n---\nAUTO\n")[0] == 200
