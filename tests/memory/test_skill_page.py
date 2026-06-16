from durin.memory.paths import skill_path_from_uri, skill_uri, walk_skills
from durin.memory.skill_page import SkillPage


def _mk(ws, name, desc="does things", body="Step 1\nStep 2\n", mode="auto", disabled=False):
    d = ws / "skills" / name; d.mkdir(parents=True)
    dis = "    disable_model_invocation: true\n" if disabled else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  durin:\n"
        f"    mode: {mode}\n{dis}---\n{body}", encoding="utf-8")


def test_skill_page_parses_frontmatter_and_body(tmp_path):
    _mk(tmp_path, "git-helper", desc="git rebase flow", body="do rebase\n")
    sp = SkillPage.from_file(tmp_path / "skills" / "git-helper" / "SKILL.md")
    assert sp is not None
    assert sp.name == "git-helper"
    assert sp.description == "git rebase flow"
    assert "do rebase" in sp.body
    assert sp.mode == "auto"
    assert sp.disabled is False


def test_skill_page_none_for_missing(tmp_path):
    assert SkillPage.from_file(tmp_path / "nope" / "SKILL.md") is None


def test_skill_page_reads_tombstone_flag(tmp_path):
    _mk(tmp_path, "dead", disabled=True)
    sp = SkillPage.from_file(tmp_path / "skills" / "dead" / "SKILL.md")
    assert sp is not None and sp.disabled is True


def test_walk_skills_finds_all_and_skips_underscore(tmp_path):
    _mk(tmp_path, "a"); _mk(tmp_path, "b")
    (tmp_path / "skills" / "_scratch").mkdir(parents=True)
    (tmp_path / "skills" / "_scratch" / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    assert sorted(p.parent.name for p in walk_skills(tmp_path)) == ["a", "b"]


def test_uri_helpers_roundtrip():
    assert skill_uri("git-helper") == "skill/git-helper"
    assert skill_path_from_uri("skill/git-helper") == "skills/git-helper/SKILL.md"
