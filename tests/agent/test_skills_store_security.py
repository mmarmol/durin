from pathlib import Path

import pytest

from durin.agent import skills_store as ss


def _user_skill(ws: Path, name: str, body: str = "Body\n") -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n{body}", encoding="utf-8"
    )


def _sentinel(tmp_path: Path) -> Path:
    # a file OUTSIDE the workspace skills dir that must never be touched
    p = tmp_path / "SECRET.txt"
    p.write_text("ORIGINAL", encoding="utf-8")
    return p


def test_apply_edit_rejects_traversal_name(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    secret = _sentinel(tmp_path)
    res = ss.apply_skill_edit(ws, "../../SECRET", old="ORIGINAL", new="PWNED",
                              rationale="r", confirm=True, file="../SECRET.txt")
    assert "error" in res
    assert secret.read_text(encoding="utf-8") == "ORIGINAL"


def test_apply_edit_rejects_traversal_file(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", body="step\n")
    ss.set_mode(ws, "mine", "auto")
    escaped = ws / "escaped.txt"
    res = ss.apply_skill_edit(ws, "mine", old="", new="PWNED", rationale="r",
                              confirm=True, file="../../escaped.txt")
    assert "error" in res
    assert not (tmp_path / "escaped.txt").exists()
    assert not escaped.exists()


def test_apply_edit_allows_scripts_subdir_file(tmp_path):
    # legitimate nested file under the skill dir must STILL work
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", body="step\n")
    ss.set_mode(ws, "mine", "auto")
    res = ss.apply_skill_edit(ws, "mine", old="", new="print('hi')\n",
                              rationale="add script", confirm=True,
                              file="scripts/run.py")
    assert res["ok"] is True
    assert (ws / "skills" / "mine" / "scripts" / "run.py").exists()


def test_set_mode_rejects_traversal_name(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(FileNotFoundError):
        ss.set_mode(ws, "..", "manual")


def test_web_mode_rejects_traversal_name(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "SKILL.md").write_text("---\nname: x\ndescription: d\n---\nROOT\n", encoding="utf-8")
    status, _ = ss.web_mode(ws, "..", "manual")
    assert status in (400, 404)
    # the workspace-root SKILL.md must be untouched
    assert "ROOT" in (ws / "SKILL.md").read_text(encoding="utf-8")


def test_web_save_and_get_reject_traversal_name(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert ss.web_save(ws, "..", "x")[0] in (400, 404)
    assert ss.web_get(ws, "..")[0] == 404
