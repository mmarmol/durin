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


def test_save_skill_content_editable_in_both_modes(tmp_path):
    # auto is not a user lock: the web save works in either mode.
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    ok = ss.save_skill_content(ws, "mine", "---\nname: mine\ndescription: d\n---\nNEW\n")
    assert ok["ok"] is True
    ss.set_mode(ws, "mine", "auto")
    # The web editor round-trips the full frontmatter, so the mode is preserved.
    auto = ss.save_skill_content(
        ws, "mine",
        "---\nname: mine\ndescription: d\nmetadata:\n  durin:\n    mode: auto\n---\nAUTO\n")
    assert auto["ok"] is True
    assert ss.read_mode(ws, "mine") == "auto"  # a user edit leaves it auto


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


def test_create_derives_missing_frontmatter(tmp_path):
    from durin.agent.skills_store import dream_create_skill, read_skill_content
    body = (
        "# Weather Lookup\n\n"
        "Look up current weather for a location.\n\n"
        "## Triggers\n\n- user asks about weather\n- forecast questions\n"
    )
    out = dream_create_skill(tmp_path, "weather-lookup", body, "test create")
    assert out.get("ok"), out
    text = read_skill_content(tmp_path, "weather-lookup")
    assert "name: weather-lookup" in text
    assert "description:" in text
    # derived description carries prose + trigger text so the skill can surface
    assert "weather" in text.split("---")[1].lower()


def test_create_keeps_existing_frontmatter(tmp_path):
    from durin.agent.skills_store import dream_create_skill, read_skill_content
    content = (
        "---\nname: my-skill\ndescription: An explicit description with triggers.\n---\n"
        "# My Skill\n\nBody.\n"
    )
    out = dream_create_skill(tmp_path, "my-skill", content, "test create")
    assert out.get("ok"), out
    text = read_skill_content(tmp_path, "my-skill")
    assert "An explicit description with triggers." in text


def test_create_rejects_underivable_body(tmp_path):
    from durin.agent.skills_store import dream_create_skill
    out = dream_create_skill(tmp_path, "empty-skill", "   \n", "test create")
    assert out.get("error")


def test_fuse_derives_missing_frontmatter(tmp_path):
    from durin.agent.skills_store import dream_create_skill, dream_fuse_skills, read_skill_content
    a = "---\nname: a\ndescription: d-a\n---\n# A\n\nProc A.\n"
    b = "---\nname: b\ndescription: d-b\n---\n# B\n\nProc B.\n"
    assert dream_create_skill(tmp_path, "a", a, "seed").get("ok")
    assert dream_create_skill(tmp_path, "b", b, "seed").get("ok")
    merged = "# AB\n\nMerged procedure for A and B.\n"
    out = dream_fuse_skills(tmp_path, target="ab", content=merged,
                            sources=["a", "b"], rationale="merge")
    assert out.get("ok"), out
    text = read_skill_content(tmp_path, "ab")
    assert "name: ab" in text and "description:" in text


def test_derived_description_collapses_wrapped_paragraphs(tmp_path):
    """A wrapped markdown paragraph must not land verbatim in the frontmatter:
    the derived copy collapses newlines so exact-match edits targeting the
    paragraph still find exactly one occurrence in the file."""
    from durin.agent.skills_store import apply_skill_edit, dream_create_skill
    para = "Convert PDF files to Markdown format\nwhile preserving headings and tables."
    body = f"# PDF Convert\n\n{para}\n\n## Triggers\n\n- pdf attached\n"
    assert dream_create_skill(tmp_path, "pdf-convert", body, "seed").get("ok")
    out = apply_skill_edit(tmp_path, "pdf-convert", old=para,
                           new="Convert PDFs to clean Markdown.",
                           rationale="test edit")
    assert out.get("ok"), out
