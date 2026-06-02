# tests/agent/test_skills_store_write.py
from pathlib import Path

import pytest

from durin.agent import skills_store as ss


def _user_skill(ws: Path, name: str, body: str = "Body\n") -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n{body}", encoding="utf-8"
    )


def test_set_mode_writes_frontmatter_and_commits(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    sha = ss.set_mode(ws, "mine", "auto")
    assert sha is not None
    assert ss.read_mode(ws, "mine") == "auto"


def test_apply_edit_auto_skill_commits(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", body="step one\n")
    ss.set_mode(ws, "mine", "auto")
    res = ss.apply_skill_edit(
        ws, "mine", old="step one", new="step ONE (better)", rationale="clarify step",
    )
    assert res["ok"] is True
    assert res["commit"]
    assert "step ONE (better)" in (ws / "skills" / "mine" / "SKILL.md").read_text()


def test_apply_edit_manual_without_confirm_proposes_only(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", body="step one\n")
    res = ss.apply_skill_edit(ws, "mine", old="step one", new="x", rationale="r")
    assert res.get("proposed") is True
    assert "preview" in res
    assert "step one" in (ws / "skills" / "mine" / "SKILL.md").read_text()


def test_apply_edit_manual_with_confirm_writes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", body="step one\n")
    res = ss.apply_skill_edit(ws, "mine", old="step one", new="step two", rationale="r", confirm=True)
    assert res["ok"] is True
    assert res["commit"]
    assert "step two" in (ws / "skills" / "mine" / "SKILL.md").read_text()


def test_apply_edit_rejects_missing_rationale_and_bad_match(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    assert "error" in ss.apply_skill_edit(ws, "mine", old="x", new="y", rationale="  ")
    assert "error" in ss.apply_skill_edit(ws, "mine", old="NOPE", new="y", rationale="r", confirm=True)


def test_apply_edit_rejects_non_unique_old(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", body="dup\ndup\n")  # 'dup' appears twice
    res = ss.apply_skill_edit(ws, "mine", old="dup", new="x", rationale="r", confirm=True)
    assert "error" in res
    assert "unique" in res["error"]


def test_save_skill_content_requires_manual(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    ok = ss.save_skill_content(ws, "mine", "---\nname: mine\ndescription: d\n---\nNEW\n")
    assert ok["ok"] is True
    ss.set_mode(ws, "mine", "auto")
    rej = ss.save_skill_content(ws, "mine", "whatever")
    assert "error" in rej


def test_apply_edit_create_file_with_empty_old(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", body="step one\n")
    ss.set_mode(ws, "mine", "auto")
    res = ss.apply_skill_edit(ws, "mine", old="", new="\nappended line\n", rationale="add note")
    assert res["ok"] is True
    assert "appended line" in (ws / "skills" / "mine" / "SKILL.md").read_text()


def test_save_skill_content_returns_commit_sha(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    res = ss.save_skill_content(ws, "mine", "---\nname: mine\ndescription: d\n---\nNEW\n")
    assert res["ok"] is True
    assert res["commit"]  # truthy SHA


def test_set_mode_raises_on_invalid_mode(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    with pytest.raises(ValueError):
        ss.set_mode(ws, "mine", "bogus")
